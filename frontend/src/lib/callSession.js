/**
 * Shell side of `media.call` v1: live calls for opaque apps.
 *
 * The shell holds the microphone, camera, any shared screen, every
 * RTCPeerConnection, Web Audio playback and the painted video tiles. The app
 * relays the opaque `signal` payloads between participants and steers volumes
 * and tile rectangles; every value it sees is plain JSON.
 *
 * Each connection has one audio and one video sender from the start: the
 * impolite side offers them, the polite side answers. Muting, a device turned
 * on later and a shared screen (which takes the camera's place) only swap the
 * track being sent, so a connection never renegotiates except to restart ICE.
 * A negotiated data channel carries each side's `{audio, video, screen}` state,
 * so a camera turned off clears its tile instead of painting black frames.
 */

export const MEDIA_CALL = 'media.call'
const SELF = 'self'

const PEER_ID = /^[A-Za-z0-9_.:~@-]{1,80}$/
const ICE_URL = /^(?:stun|turns?):\S+$/
const DEFAULT_MAX_PEERS = 8
const HARD_MAX_PEERS = 32
const MAX_ICE_SERVERS = 4
const MAX_SDP = 100 * 1024
const MAX_CANDIDATE = 2048
const MAX_FIELD = 256
const MAX_GAINS = 64
const MAX_PENDING = 256
const MAX_COORDINATE = 100_000
const LEVEL_INTERVAL_MS = 200
const STATE_CHANNEL_ID = 0
const CONTROLS = new Set(['connect', 'signal', 'disconnect', 'volume', 'tiles', 'local', 'screen'])
const CONNECTED = new Set(['connected', 'completed'])
const ENDED = new Set(['disconnected', 'failed', 'closed'])
// Screens favour legible detail over motion, capped at 1080p for the mesh.
const DISPLAY = { video: { frameRate: { ideal: 15, max: 30 }, width: { max: 1920 }, height: { max: 1080 } }, audio: false }

// --- Checking what the app sends ---------------------------------------------------------

function callError(code, message, name = 'CapabilityError') {
  return Object.assign(new Error(message), { name, code })
}

const invalid = (message) => callError('invalid_request', message, 'TypeError')
const isNumber = (value) => typeof value === 'number' && Number.isFinite(value)
const clamp = (value, low, high) => Math.min(high, Math.max(low, value))
const isText = (value, max) => typeof value === 'string' && value.length <= max

function isPlainObject(value) {
  if (!value || typeof value !== 'object') return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function fields(value, allowed, label) {
  if (!isPlainObject(value)) throw invalid(`${label} must be an object.`)
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key))
  if (unknown.length) throw invalid(`Unknown ${label} field: ${unknown.slice(0, 4).map((k) => k.slice(0, 40)).join(', ')}.`)
  return value
}

const readPeer = (value) => (typeof value === 'string' && value !== SELF && PEER_ID.test(value) ? value : null)

function requirePeer(value) {
  const id = readPeer(value)
  if (!id) throw invalid('A call peer id must be 1-80 letters, digits, or `_.:~@-` characters, and not `self`.')
  return id
}

function readIceServer(entry, index) {
  fields(entry, ['urls', 'username', 'credential'], `iceServers[${index}]`)
  const urls = typeof entry.urls === 'string' ? [entry.urls] : entry.urls
  if (!Array.isArray(urls) || !urls.length || urls.length > 4 || !urls.every((u) => isText(u, 512) && ICE_URL.test(u))) {
    throw invalid(`iceServers[${index}].urls must be 1-4 stun:, turn:, or turns: URLs of at most 512 characters.`)
  }
  const server = { urls: [...urls] }
  for (const key of ['username', 'credential']) {
    if (entry[key] === undefined) continue
    if (!isText(entry[key], MAX_FIELD)) throw invalid(`iceServers[${index}].${key} must be a string of at most ${MAX_FIELD} characters.`)
    server[key] = entry[key]
  }
  if (urls.some((u) => u.startsWith('turn')) && !(server.username && server.credential)) {
    throw invalid(`iceServers[${index}] needs a username and credential for its TURN URLs.`)
  }
  return server
}

function readRequest(input, declaration) {
  fields(input ?? {}, ['audio', 'video', 'iceServers'], 'media.call input')
  const { audio = true, video = false, iceServers = [] } = input ?? {}
  if (typeof audio !== 'boolean' || typeof video !== 'boolean') throw invalid('Call `audio` and `video` must be true or false.')
  if (!Array.isArray(iceServers) || iceServers.length > MAX_ICE_SERVERS) {
    throw invalid(`Call \`iceServers\` must be an array of at most ${MAX_ICE_SERVERS} servers.`)
  }
  const reviewed = Math.floor(Number(declaration?.limits?.max_peers))
  return {
    audio,
    video,
    iceServers: iceServers.map(readIceServer),
    maxPeers: reviewed >= 1 ? Math.min(HARD_MAX_PEERS, reviewed) : DEFAULT_MAX_PEERS,
  }
}

function readSignal(data) {
  const keys = isPlainObject(data) ? Object.keys(data) : []
  if (keys.length !== 1 || !['description', 'candidate'].includes(keys[0])) {
    throw invalid('Call signal `data` must contain exactly one `description` or `candidate`.')
  }
  if (data.description) {
    const { type, sdp } = fields(data.description, ['type', 'sdp'], 'call description')
    if ((type !== 'offer' && type !== 'answer') || !sdp || !isText(sdp, MAX_SDP)) {
      throw invalid(`A call description needs type \`offer\` or \`answer\` and an sdp of 1-${MAX_SDP} characters.`)
    }
    return { description: { type, sdp } }
  }
  if (data.candidate === null) return { candidate: null }
  const { candidate, sdpMid, sdpMLineIndex, usernameFragment } = fields(
    data.candidate, ['candidate', 'sdpMid', 'sdpMLineIndex', 'usernameFragment'], 'call candidate',
  )
  const optional = (value) => value === undefined || value === null || isText(value, MAX_FIELD)
  const index = sdpMLineIndex === undefined || sdpMLineIndex === null
    || (Number.isInteger(sdpMLineIndex) && sdpMLineIndex >= 0 && sdpMLineIndex <= 65_535)
  if (!isText(candidate, MAX_CANDIDATE) || !optional(sdpMid) || !optional(usernameFragment) || !index) {
    throw invalid('A call candidate has a field of the wrong type or size.')
  }
  const value = { candidate }
  if (sdpMid !== undefined) value.sdpMid = sdpMid
  if (sdpMLineIndex !== undefined) value.sdpMLineIndex = sdpMLineIndex
  if (usernameFragment !== undefined) value.usernameFragment = usernameFragment
  return { candidate: value }
}

function readTiles(value, maxTiles) {
  fields(value, ['tiles'], 'call tiles control')
  if (!Array.isArray(value.tiles)) throw invalid('Call `tiles` must be an array.')
  if (value.tiles.length > maxTiles) throw callError('limit_exceeded', `At most ${maxTiles} call tiles can be painted.`, 'RangeError')
  return value.tiles.map((tile, i) => {
    const label = `tiles[${i}]`
    fields(tile, ['peer', 'x', 'y', 'width', 'height', 'radius', 'mirror', 'opacity'], label)
    const peer = tile.peer === SELF ? SELF : readPeer(tile.peer)
    if (!peer) throw invalid(`${label}.peer must be \`self\` or a call peer id.`)
    if (![tile.x, tile.y, tile.width, tile.height].every(isNumber) || tile.width <= 0 || tile.height <= 0) {
      throw invalid(`${label} needs finite x and y and a positive width and height.`)
    }
    if (![tile.radius, tile.opacity].every((v) => v === undefined || isNumber(v))) throw invalid(`${label} radius and opacity must be numbers.`)
    if (tile.mirror !== undefined && typeof tile.mirror !== 'boolean') throw invalid(`${label}.mirror must be true or false.`)
    return {
      peer,
      x: clamp(tile.x, -MAX_COORDINATE, MAX_COORDINATE),
      y: clamp(tile.y, -MAX_COORDINATE, MAX_COORDINATE),
      width: Math.min(tile.width, MAX_COORDINATE),
      height: Math.min(tile.height, MAX_COORDINATE),
      radius: clamp(tile.radius ?? 0, 0, MAX_COORDINATE),
      mirror: tile.mirror,
      opacity: clamp(tile.opacity ?? 1, 0, 1),
    }
  })
}

// --- Media helpers -----------------------------------------------------------------------

const deniedMedia = (error) => ['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(error?.name)

function mediaFailure(errors) {
  const denied = errors.find(deniedMedia)
  return denied
    ? callError('denied', 'Microphone or camera access was denied. Allow it for this site in the browser, then try again.', 'NotAllowedError')
    : callError('unavailable', 'No usable microphone or camera was found, or another app is using it.', 'NotFoundError')
}

function screenFailure(error) {
  if (error?.name === 'InvalidStateError') {
    return callError('denied', 'Screen sharing must start from a click or key press in the app.', 'NotAllowedError')
  }
  return deniedMedia(error)
    ? callError('denied', 'Screen sharing was cancelled or blocked.', 'NotAllowedError')
    : callError('unavailable', 'No screen could be shared from this device.', 'NotFoundError')
}

function mediaConstraints(audio, video) {
  return {
    audio: audio && { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    video: video && { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 360 }, frameRate: { ideal: 24 } },
  }
}

const live = (track) => Boolean(track) && track.readyState !== 'ended' && track.enabled !== false
const receiving = (track) => Boolean(track) && track.readyState !== 'ended' && !track.muted

function stopTracks(stream) {
  for (const track of stream?.getTracks?.() || []) {
    track.onended = null
    try { track.stop() } catch { /* already stopped */ }
  }
}

function disconnect(...nodes) {
  for (const node of nodes) {
    try { node?.disconnect?.() } catch { /* already disconnected */ }
  }
}

function createMeter(audioContext, stream) {
  const source = audioContext.createMediaStreamSource(stream)
  const analyser = audioContext.createAnalyser()
  analyser.fftSize = 512
  source.connect(analyser)
  return { source, analyser, samples: new Float32Array(analyser.fftSize), smoothed: 0 }
}

// A smoothed 0..1 speaking level: -60 dBFS is silence and -10 dBFS full. It
// rises fast and falls slowly, so meters do not flicker between words.
function measure(meter) {
  meter.analyser.getFloatTimeDomainData(meter.samples)
  let sum = 0
  for (const sample of meter.samples) sum += sample * sample
  const rms = Math.sqrt(sum / meter.samples.length)
  const raw = rms > 0 ? clamp((20 * Math.log10(rms) + 60) / 50, 0, 1) : 0
  meter.smoothed = raw >= meter.smoothed ? raw * 0.7 + meter.smoothed * 0.3 : raw * 0.35 + meter.smoothed * 0.65
  return Math.round(meter.smoothed * 100) / 100
}

function candidateSignal(candidate) {
  if (!candidate) return { candidate: null }
  const { candidate: text = '', sdpMid, sdpMLineIndex, usernameFragment } = candidate.toJSON?.() ?? candidate
  const value = { candidate: text }
  if (typeof sdpMid === 'string') value.sdpMid = sdpMid
  if (Number.isInteger(sdpMLineIndex)) value.sdpMLineIndex = sdpMLineIndex
  if (typeof usernameFragment === 'string') value.usernameFragment = usernameFragment
  return { candidate: value }
}

// --- The session ----------------------------------------------------------------------------

function startCall(request, channel, env) {
  const { mediaDevices, RTCPeerConnectionCtor, MediaStreamCtor, AudioContextCtor, createElement, createSurface, now } = env
  if (![RTCPeerConnectionCtor, MediaStreamCtor, AudioContextCtor].every((ctor) => typeof ctor === 'function')
    || ((request.audio || request.video) && typeof mediaDevices?.getUserMedia !== 'function')) {
    throw callError('unavailable', 'Live calls are unavailable in this browser.', 'NotSupportedError')
  }
  let audioContext
  try {
    audioContext = new AudioContextCtor()
  } catch {
    throw callError('unavailable', 'Call audio is unavailable in this browser.', 'NotSupportedError')
  }
  const peers = new Map()
  const gains = new Map()
  const pending = []
  // Local tracks, the self-view streams painted for them, and requests in flight.
  const local = { audio: null, video: null, screen: null }
  const selfView = { video: null, screen: null }
  const asking = new Map() // kind -> the on/off the app wants once the device arrives
  let screenAsk = null
  let selfMeter = null
  let phase = 'starting'
  let startedAt = 0
  let levelTimer = null
  let tiles = []
  let surface = null
  let playback = audioContext.state === 'running' ? 'running' : 'suspended'

  const current = (peer) => phase === 'live' && peers.get(peer.id) === peer

  function report(peer, error) {
    if (phase === 'ended') return
    channel.event('error', {
      peer: peer || null,
      code: typeof error?.code === 'string' ? error.code : 'provider_error',
      message: typeof error?.message === 'string' && error.message ? error.message : 'The call hit an unexpected problem.',
    })
  }

  function notePlayback() {
    const next = audioContext.state === 'running' ? 'running' : 'suspended'
    if (phase === 'ended' || next === playback) return
    playback = next
    if (phase === 'live') channel.event('playback', { state: next })
  }

  function resumeAudio() {
    if (phase === 'ended' || audioContext.state !== 'suspended') return
    Promise.resolve(audioContext.resume?.()).then(notePlayback, () => {})
  }

  // --- local media

  const localState = () => ({ audio: live(local.audio), video: live(local.video), screen: Boolean(local.screen) })

  function sendTracks(peer) {
    if (!peer.senders) return
    const sent = { audio: local.audio, video: local.screen || local.video }
    for (const kind of ['audio', 'video']) {
      peer.senders[kind].replaceTrack(sent[kind] || null).catch(() => {})
    }
  }

  function announce(peer) {
    if (peer.channel?.readyState !== 'open') return
    try { peer.channel.send(JSON.stringify(localState())) } catch { /* closing */ }
  }

  function localChanged() {
    if (phase !== 'live') return
    for (const peer of peers.values()) {
      sendTracks(peer)
      announce(peer)
    }
    channel.event('local', localState())
    repaint()
  }

  function adoptTrack(kind, track) {
    local[kind] = track
    track.onended = localChanged
    if (kind === 'audio') {
      try { selfMeter = createMeter(audioContext, new MediaStreamCtor([track])) } catch { selfMeter = null }
    } else {
      selfView.video = new MediaStreamCtor([track])
    }
  }

  async function acquireMedia() {
    if (!request.audio && !request.video) return {}
    const failures = []
    const attempt = (audio, video) => mediaDevices.getUserMedia(mediaConstraints(audio, video))
      .catch((error) => { failures.push(error); return null })
    let stream = await attempt(request.audio, request.video)
    if (stream) return { stream }
    if (!request.audio || !request.video) throw mediaFailure(failures)
    // Keep whichever half works. A refused microphone is not asked about again.
    stream = await attempt(true, false)
    if (stream) return { stream, videoError: deniedMedia(failures[0]) ? 'denied' : 'unavailable' }
    if (deniedMedia(failures[1])) throw mediaFailure(failures)
    stream = await attempt(false, true)
    if (stream) return { stream, audioError: deniedMedia(failures[1]) ? 'denied' : 'unavailable' }
    throw mediaFailure(failures)
  }

  function begin({ stream, audioError, videoError }) {
    if (phase !== 'starting') {
      stopTracks(stream) // Cancelled while the permission prompt was open.
      return
    }
    const [audio] = stream?.getAudioTracks() || []
    const [video] = stream?.getVideoTracks() || []
    if (audio) adoptTrack('audio', audio)
    if (video) adoptTrack('video', video)
    phase = 'live'
    startedAt = now()
    levelTimer = env.startInterval(emitLevels, LEVEL_INTERVAL_MS)
    channel.ready({ audio: Boolean(audio), video: Boolean(video), playback, ...(audioError && { audioError }), ...(videoError && { videoError }) })
    for (const [action, value] of pending.splice(0)) apply(action, value)
    resumeAudio()
  }

  // A kind the call has no device for is asked for when the app turns it on;
  // the latest on or off applies when the device arrives.
  function askDevice(kind, on) {
    if (asking.has(kind)) {
      asking.set(kind, on)
      return
    }
    if (!on) return
    asking.set(kind, true)
    Promise.resolve().then(() => mediaDevices.getUserMedia(mediaConstraints(kind === 'audio', kind === 'video'))).then((stream) => {
      const wanted = asking.get(kind)
      asking.delete(kind)
      const track = stream?.getTracks().find((t) => t.kind === kind)
      if (phase !== 'live' || !track || local[kind]) {
        stopTracks(stream)
        if (phase === 'live' && !track) report(null, mediaFailure([]))
        return
      }
      for (const other of stream.getTracks()) if (other !== track) other.stop()
      track.enabled = wanted
      adoptTrack(kind, track)
      localChanged()
    }, (error) => {
      asking.delete(kind)
      if (phase === 'live') report(null, mediaFailure([error]))
    })
  }

  function setLocal(value) {
    fields(value, ['audio', 'video'], 'call local control')
    for (const kind of ['audio', 'video']) {
      if (value[kind] === undefined) continue
      if (typeof value[kind] !== 'boolean') throw invalid(`Call local \`${kind}\` must be true or false.`)
      if (local[kind]) local[kind].enabled = value[kind]
      else askDevice(kind, value[kind])
    }
    localChanged()
  }

  function stopScreen() {
    screenAsk = null
    const track = local.screen
    if (!track) return
    local.screen = null
    selfView.screen = null
    stopTracks({ getTracks: () => [track] })
    localChanged()
  }

  function shareScreen(value) {
    fields(value, ['share'], 'call screen control')
    if (typeof value.share !== 'boolean') throw invalid('Call `screen` needs a boolean `share`.')
    if (!value.share) return stopScreen()
    // One screen per call: asking again while sharing or choosing does nothing.
    if (local.screen || screenAsk) return
    if (typeof mediaDevices?.getDisplayMedia !== 'function') {
      throw callError('unavailable', 'Screen sharing is unavailable in this browser.', 'NotSupportedError')
    }
    const ask = {}
    screenAsk = ask
    // Called at once: the click in the app frame that sent this control also
    // activated the shell, and the browser opens its picker only for that.
    new Promise((resolve) => resolve(mediaDevices.getDisplayMedia(DISPLAY))).then((stream) => {
      const track = stream?.getVideoTracks()[0]
      if (screenAsk !== ask || phase !== 'live' || !track) {
        stopTracks(stream)
        if (screenAsk === ask) {
          screenAsk = null
          report(null, screenFailure(null))
        }
        return
      }
      screenAsk = null
      for (const other of stream.getTracks()) if (other !== track) other.stop()
      if ('contentHint' in track) track.contentHint = 'detail'
      local.screen = track
      selfView.screen = new MediaStreamCtor([track])
      // The browser's own "Stop sharing" button ends the track.
      track.onended = stopScreen
      localChanged()
    }, (error) => {
      if (screenAsk !== ask) return
      screenAsk = null
      report(null, screenFailure(error))
    })
  }

  // --- peers

  function snapshot(peer) {
    const { pc, remote } = peer
    const raw = pc.connectionState ?? pc.iceConnectionState
    const state = CONNECTED.has(raw) ? 'connected' : ENDED.has(raw) ? raw : 'connecting'
    const flowing = state === 'connected'
    const video = flowing && (remote.video || remote.screen) && receiving(peer.video?.track)
    return {
      peer: peer.id,
      state,
      audio: flowing && remote.audio && receiving(peer.audio?.track),
      video,
      screen: video && remote.screen,
    }
  }

  function refreshPeer(peer) {
    if (!current(peer)) return
    const value = snapshot(peer)
    const key = `${value.state}|${value.audio}|${value.video}|${value.screen}`
    if (peer.reported === key) return
    peer.reported = key
    channel.event('peer', value)
    repaint()
  }

  function receiveState(peer, data) {
    let state
    try { state = JSON.parse(isText(data, 128) ? data : 'null') } catch { return }
    if (!current(peer) || !isPlainObject(state) || !['audio', 'video', 'screen'].every((k) => typeof state[k] === 'boolean')) return
    peer.remote = { audio: state.audio, video: state.video, screen: state.screen }
    refreshPeer(peer)
  }

  function playRemoteAudio(peer, track) {
    const stream = new MediaStreamCtor([track])
    // Chrome feeds a remote stream into Web Audio only while a media element
    // also plays it. The element stays muted; sound comes from the gain node.
    const sink = createElement('audio')
    if (sink) {
      sink.muted = true
      sink.srcObject = stream
      sink.play?.()?.catch?.(() => {})
    }
    const meter = createMeter(audioContext, stream)
    const gain = audioContext.createGain()
    gain.gain.value = gains.get(peer.id) ?? 1
    meter.source.connect(gain)
    gain.connect(audioContext.destination)
    peer.audio = { track, sink, gain, ...meter }
    resumeAudio()
  }

  function receiveTrack(peer, track) {
    if (!current(peer) || !track) return
    if (track.kind === 'audio' && peer.audio?.track !== track) {
      try { playRemoteAudio(peer, track) } catch { report(peer.id, callError('provider_error', 'This person\'s audio could not be played.')) }
    } else if (track.kind === 'video' && peer.video?.track !== track) {
      peer.video = { track, stream: new MediaStreamCtor([track]) }
    }
    track.onmute = track.onunmute = track.onended = () => refreshPeer(peer)
    refreshPeer(peer)
  }

  async function negotiate(peer) {
    try {
      await peer.pc.setLocalDescription()
      if (current(peer)) channel.event('signal', { peer: peer.id, data: { description: { type: peer.pc.localDescription.type, sdp: peer.pc.localDescription.sdp } } })
    } catch {
      if (current(peer)) report(peer.id, callError('provider_error', 'The call connection could not be negotiated.'))
    }
  }

  function createPeer(id, polite) {
    if (peers.size >= request.maxPeers) {
      throw callError('limit_exceeded', `This app can connect to at most ${request.maxPeers} people at once.`, 'RangeError')
    }
    const pc = new RTCPeerConnectionCtor({ iceServers: request.iceServers })
    const peer = { id, pc, polite, senders: null, remote: { audio: true, video: true, screen: false }, audio: null, video: null, channel: null, queue: Promise.resolve(), reported: '' }
    peers.set(id, peer)
    pc.onicecandidate = (event) => {
      if (current(peer)) channel.event('signal', { peer: id, data: candidateSignal(event?.candidate) })
    }
    pc.ontrack = (event) => receiveTrack(peer, event?.track)
    pc.onconnectionstatechange = () => refreshPeer(peer)
    pc.oniceconnectionstatechange = () => {
      // Only the offering side restarts ICE, so the two sides never offer at once.
      if (current(peer) && pc.iceConnectionState === 'failed' && !peer.polite) pc.restartIce?.()
      refreshPeer(peer)
    }
    pc.onnegotiationneeded = () => {
      if (current(peer) && !peer.polite) void negotiate(peer)
    }
    try {
      peer.channel = pc.createDataChannel('mobius-call-state', { negotiated: true, id: STATE_CHANNEL_ID })
      peer.channel.onopen = () => announce(peer)
      peer.channel.onmessage = (event) => receiveState(peer, event?.data)
    } catch { /* remote media falls back to track liveness */ }
    if (!polite) {
      peer.senders = {
        audio: pc.addTransceiver('audio', { direction: 'sendrecv' }).sender,
        video: pc.addTransceiver('video', { direction: 'sendrecv' }).sender,
      }
      sendTracks(peer)
    }
    refreshPeer(peer)
    return peer
  }

  function closePeer(peer) {
    const { pc } = peer
    pc.onicecandidate = pc.ontrack = pc.onconnectionstatechange = pc.oniceconnectionstatechange = pc.onnegotiationneeded = null
    if (peer.channel) {
      peer.channel.onopen = peer.channel.onmessage = null
      try { peer.channel.close() } catch { /* already closed */ }
    }
    for (const track of [peer.audio?.track, peer.video?.track]) if (track) track.onmute = track.onunmute = track.onended = null
    if (peer.audio) {
      disconnect(peer.audio.source, peer.audio.analyser, peer.audio.gain)
      if (peer.audio.sink) peer.audio.sink.srcObject = null
    }
    try { pc.close() } catch { /* already closed */ }
  }

  async function receiveSignal(peer, { description, candidate }) {
    const { pc } = peer
    if (!current(peer)) return
    if (!description) {
      // End-of-candidates is optional; ICE completes without it.
      if (candidate) await pc.addIceCandidate(candidate)
      return
    }
    // The impolite side only offers and the polite side only answers.
    if ((description.type === 'offer') !== peer.polite) {
      report(peer.id, invalid('Both sides of a call connection passed the same `polite` value.'))
      return
    }
    await pc.setRemoteDescription(description)
    if (description.type === 'answer' || !current(peer)) return
    if (!peer.senders) {
      // Send on the transceivers the offer created.
      const senderFor = (kind) => {
        const transceiver = pc.getTransceivers().find((t) => t.receiver?.track?.kind === kind)
        if (transceiver) transceiver.direction = 'sendrecv'
        return transceiver?.sender
      }
      peer.senders = { audio: senderFor('audio'), video: senderFor('video') }
      if (!peer.senders.audio || !peer.senders.video) {
        peer.senders = null
        throw invalid('A call offer must carry one audio and one video section.')
      }
      sendTracks(peer)
    }
    await negotiate(peer)
  }

  // --- controls

  function connect(value) {
    fields(value, ['peer', 'polite'], 'call connect control')
    const id = requirePeer(value.peer)
    if (typeof value.polite !== 'boolean') throw invalid('Call `connect` needs a boolean `polite` flag.')
    if (!peers.has(id)) createPeer(id, value.polite)
  }

  function signal(value) {
    fields(value, ['peer', 'data'], 'call signal control')
    const id = requirePeer(value.peer)
    const data = readSignal(value.data)
    let peer = peers.get(id)
    if (!peer) {
      // Only an offer opens a connection; anything else for an unknown peer is stale.
      if (data.description?.type !== 'offer') return
      peer = createPeer(id, true)
    }
    peer.queue = peer.queue.then(() => receiveSignal(peer, data)).catch((error) => {
      // Our own errors carry a string code; browser errors get a stable message.
      if (current(peer)) report(id, typeof error?.code === 'string' ? error : invalid('The browser rejected a call signal from this person.'))
    })
  }

  function disconnectPeer(value) {
    fields(value, ['peer'], 'call disconnect control')
    const id = requirePeer(value.peer)
    gains.delete(id)
    const peer = peers.get(id)
    if (!peer) return
    peers.delete(id)
    closePeer(peer)
    channel.event('peer', { peer: id, state: 'closed', audio: false, video: false, screen: false })
    repaint()
  }

  function setVolumes(value) {
    fields(value, ['gains'], 'call volume control')
    if (!isPlainObject(value.gains)) throw invalid('Call `gains` must map peer ids to numbers.')
    for (const [key, requested] of Object.entries(value.gains)) {
      const id = readPeer(key)
      if (!id || !isNumber(requested)) {
        report(id, invalid('Call volumes map peer ids to numbers from 0 to 1.'))
      } else if (!gains.has(id) && gains.size >= MAX_GAINS) {
        report(id, callError('limit_exceeded', `At most ${MAX_GAINS} call volumes are remembered.`, 'RangeError'))
      } else {
        // Remembered before a peer's audio arrives, so it starts at this level.
        gains.set(id, clamp(requested, 0, 1))
        const node = peers.get(id)?.audio?.gain
        node?.gain.setTargetAtTime(gains.get(id), audioContext.currentTime, 0.05)
      }
    }
  }

  const snapshotSelf = () => ({ video: Boolean(local.screen) || live(local.video), screen: Boolean(local.screen) })

  function repaint() {
    if (phase !== 'live') return
    const painted = []
    for (const tile of tiles) {
      // Your own tile shows what you send: the shared screen, else the camera.
      const self = tile.peer === SELF
      const peer = !self && peers.get(tile.peer)
      const shown = self ? snapshotSelf() : peer && snapshot(peer)
      if (!shown?.video) continue
      const stream = self ? (selfView.screen || selfView.video) : peer.video.stream
      // A screen is letterboxed rather than cropped, and never mirrored.
      painted.push({ ...tile, stream, fit: shown.screen ? 'contain' : 'cover', mirror: tile.mirror ?? (self && !shown.screen) })
    }
    if (!surface && painted.length) surface = createSurface?.() || null
    surface?.paint(painted)
  }

  function emitLevels() {
    const peersLevels = {}
    for (const peer of peers.values()) if (peer.audio) peersLevels[peer.id] = measure(peer.audio)
    const self = selfMeter ? measure(selfMeter) : 0
    if (selfMeter || Object.keys(peersLevels).length) {
      channel.event('levels', { self: live(local.audio) ? self : 0, peers: peersLevels })
    }
  }

  function apply(action, value) {
    try {
      if (action === 'connect') connect(value)
      else if (action === 'signal') signal(value)
      else if (action === 'disconnect') disconnectPeer(value)
      else if (action === 'volume') setVolumes(value)
      else if (action === 'tiles') { tiles = readTiles(value, 2 * (request.maxPeers + 1)); repaint() }
      else if (action === 'local') setLocal(value)
      else if (action === 'screen') shareScreen(value)
    } catch (error) {
      // Bad payloads and per-peer problems never end the call.
      report(isPlainObject(value) ? readPeer(value.peer) : null, error)
    }
  }

  function teardown() {
    if (phase === 'ended') return
    phase = 'ended'
    pending.length = 0
    asking.clear()
    screenAsk = null
    if (levelTimer != null) env.stopInterval(levelTimer)
    for (const peer of peers.values()) closePeer(peer)
    peers.clear()
    surface?.destroy()
    surface = null
    if (selfMeter) disconnect(selfMeter.source, selfMeter.analyser)
    stopTracks({ getTracks: () => [local.audio, local.video, local.screen].filter(Boolean) })
    audioContext.onstatechange = null
    Promise.resolve(audioContext.close?.()).catch(() => {})
  }

  function control(action, value) {
    if (phase === 'ended') return
    if (action === 'finish') {
      const durationMs = phase === 'live' ? Math.max(0, Math.round(now() - startedAt)) : 0
      teardown()
      channel.result({ durationMs })
    } else if (action === 'cancel') {
      teardown()
      // Settles after the host's own abort, which reports its own reason.
      Promise.resolve().then(() => channel.error(callError('aborted', 'The call ended because it was cancelled or this app is no longer visible.', 'AbortError')))
    } else if (action === 'audio-resume') {
      resumeAudio()
    } else if (!CONTROLS.has(action)) {
      report(null, invalid(`Unknown media.call control \`${String(action).slice(0, 40)}\`.`))
    } else if (phase === 'starting') {
      // Connections send the local tracks, so they wait for the devices.
      if (pending.length >= MAX_PENDING) report(null, callError('limit_exceeded', 'Too many call controls arrived before the call was ready.'))
      else pending.push([action, value])
    } else {
      apply(action, value)
    }
  }

  audioContext.onstatechange = notePlayback
  // The app opens a call from its own click, which also activates the shell,
  // so an autoplay-suspended context can start now.
  resumeAudio()
  Promise.resolve().then(acquireMedia).then(begin).catch((error) => {
    if (phase === 'ended') return
    teardown()
    channel.error(error)
  })
  return { control }
}

/**
 * `media.call` v1 provider. `mediaDevices` supplies `getUserMedia` and
 * `getDisplayMedia`; `createSurface()` returns the tile painter for the frame.
 */
export function createCallProvider({
  mediaDevices = globalThis.navigator?.mediaDevices,
  RTCPeerConnectionCtor = globalThis.RTCPeerConnection,
  MediaStreamCtor = globalThis.MediaStream,
  AudioContextCtor = globalThis.AudioContext || globalThis.webkitAudioContext,
  createElement = (tag) => globalThis.document?.createElement?.(tag) || null,
  createSurface = null,
  now = () => globalThis.performance?.now?.() ?? Date.now(),
  setInterval: startInterval = (callback, ms) => globalThis.setInterval(callback, ms),
  clearInterval: stopInterval = (id) => globalThis.clearInterval(id),
} = {}) {
  const env = { mediaDevices, RTCPeerConnectionCtor, MediaStreamCtor, AudioContextCtor, createElement, createSurface, now, startInterval, stopInterval }
  return {
    version: 1,
    // One call per app frame; a hidden app leaves the call.
    exclusive: true,
    onDeactivate: 'cancel',
    open({ input, declaration, channel }) {
      return startCall(readRequest(input, declaration), channel, env)
    },
  }
}
