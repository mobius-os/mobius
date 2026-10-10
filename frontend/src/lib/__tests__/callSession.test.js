import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createCallProvider, MEDIA_CALL } from '../callSession.js'
import { createCallTileLayer } from '../callTileLayer.js'
import { createCapabilityHost } from '../capabilityHost.js'
import { builtInCapabilityProviders } from '../capabilityProviders.js'

const tick = () => new Promise((resolve) => setImmediate(resolve))
async function settle(rounds = 60) {
  for (let round = 0; round < rounds; round += 1) await tick()
}

function domError(name) {
  return Object.assign(new Error(name), { name })
}

class FakeTrack {
  constructor(kind, { remote = false } = {}) {
    this.kind = kind
    this.enabled = true
    this.contentHint = ''
    this.readyState = 'live'
    this.muted = remote
    this.level = 0
    this.stops = 0
    this.onended = this.onmute = this.onunmute = null
  }

  stop() {
    this.stops += 1
    this.readyState = 'ended'
  }

  setMuted(muted) {
    if (this.muted === muted) return
    this.muted = muted
    ;(muted ? this.onmute : this.onunmute)?.({})
  }

  end() {
    this.readyState = 'ended'
    this.onended?.({})
  }
}

class FakeStream {
  constructor(tracks = []) { this.tracks = [...tracks] }
  getTracks() { return [...this.tracks] }
  getAudioTracks() { return this.tracks.filter((track) => track.kind === 'audio') }
  getVideoTracks() { return this.tracks.filter((track) => track.kind === 'video') }
}

// `plan.audio` / `plan.video` fail that kind with the given DOMException name;
// `plan.display` fails the screen picker, and `plan.deferDisplay` keeps it open
// until the test settles `pickers`. The plan stays mutable between requests.
function fakeDevices(plan = {}) {
  const devices = {
    plan, requests: [], granted: [], displayRequests: [], displays: [], pickers: [],
    async getUserMedia(constraints) {
      devices.requests.push(constraints)
      const tracks = []
      for (const kind of ['audio', 'video']) {
        if (!constraints[kind]) continue
        if (plan[kind]) throw domError(plan[kind])
        tracks.push(new FakeTrack(kind))
      }
      const stream = new FakeStream(tracks)
      devices.granted.push(stream)
      return stream
    },
    getDisplayMedia(constraints) {
      devices.displayRequests.push(constraints)
      if (plan.deferDisplay) return new Promise((resolve, reject) => devices.pickers.push({ resolve, reject }))
      if (plan.display) return Promise.reject(domError(plan.display))
      const stream = new FakeStream([new FakeTrack('video')])
      devices.displays.push(stream)
      return Promise.resolve(stream)
    },
  }
  return devices
}

function audioContextClass(contexts, { state = 'running', resumable = true } = {}) {
  return class FakeAudioContext {
    constructor() {
      this.state = state
      this.resumable = resumable
      this.currentTime = 0
      this.destination = { inputs: [] }
      this.nodes = []
      contexts.push(this)
    }

    node(extra) {
      const node = {
        inputs: [],
        disconnected: false,
        connect(target) { target.inputs?.push(this); return target },
        disconnect() { this.disconnected = true },
        ...extra,
      }
      this.nodes.push(node)
      return node
    }

    createGain() {
      return this.node({ gain: { value: 1, setTargetAtTime(value) { this.value = value } } })
    }

    createAnalyser() {
      const analyser = this.node({ fftSize: 2048 })
      analyser.getFloatTimeDomainData = (samples) => {
        samples.fill(analyser.inputs[0]?.stream?.getTracks()[0]?.level ?? 0)
      }
      return analyser
    }

    createMediaStreamSource(stream) { return this.node({ stream }) }

    resume() {
      if (this.resumable && this.state === 'suspended') {
        this.state = 'running'
        this.onstatechange?.({})
      }
      return Promise.resolve()
    }

    close() {
      this.state = 'closed'
      return Promise.resolve()
    }
  }
}

function fakeDocument() {
  const document = {
    createElement(tag) {
      return {
        tagName: tag.toUpperCase(),
        ownerDocument: document,
        style: {},
        children: [],
        parentNode: null,
        srcObject: null,
        setAttribute() {},
        appendChild(child) { child.parentNode = this; this.children.push(child); return child },
        remove() {
          if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this)
          this.parentNode = null
        },
        play() { return Promise.resolve() },
      }
    },
  }
  return document
}

// One audio and one video transceiver per side, matched by position. A sender's
// track reaches the far receiver once both descriptions are applied and that
// direction sends; a missing or ended track leaves the far receiver muted.
function fakeRtc() {
  const instances = []

  class FakePeerConnection {
    constructor(config) {
      this.id = `pc${instances.length + 1}`
      instances.push(this)
      this.config = config
      this.transceivers = []
      this.signalingState = 'stable'
      this.connectionState = 'new'
      this.iceConnectionState = 'new'
      this.localDescription = null
      this.remote = null
      this.channel = null
      this.offers = 0
      this.restarts = 0
      this.candidates = []
      this.closed = false
    }

    transceiver(kind, direction) {
      const pc = this
      const transceiver = {
        direction,
        receiver: { track: new FakeTrack(kind, { remote: true }) },
        sender: {
          track: null,
          replaceTrack(track) {
            this.track = track
            pc.flow()
            return Promise.resolve()
          },
        },
      }
      this.transceivers.push(transceiver)
      return transceiver
    }

    addTransceiver(kind, { direction }) {
      const transceiver = this.transceiver(kind, direction)
      this.needed()
      return transceiver
    }

    getTransceivers() { return [...this.transceivers] }

    createDataChannel(_label, { id }) {
      const pc = this
      this.channel = {
        id,
        readyState: 'connecting',
        sent: [],
        send(data) {
          this.sent.push(data)
          const far = pc.remote?.channel
          setImmediate(() => { if (far?.readyState === 'open') far.onmessage?.({ data }) })
        },
        close() { this.readyState = 'closed' },
      }
      this.needed()
      return this.channel
    }

    // Like a browser, changes in one task raise a single negotiationneeded.
    needed() {
      if (this.negotiationQueued) return
      this.negotiationQueued = true
      setImmediate(() => {
        this.negotiationQueued = false
        if (!this.closed && this.signalingState === 'stable') this.onnegotiationneeded?.({})
      })
    }

    async setLocalDescription() {
      await tick()
      const type = this.signalingState === 'have-remote-offer' ? 'answer' : 'offer'
      if (type === 'offer') this.offers += 1
      const media = this.transceivers.map((t) => t.receiver.track.kind)
      this.localDescription = { type, sdp: JSON.stringify({ pc: this.id, media }) }
      this.signalingState = type === 'offer' ? 'have-local-offer' : 'stable'
      const candidate = { candidate: `candidate:${this.id}`, sdpMid: '0', sdpMLineIndex: 0 }
      setImmediate(() => this.onicecandidate?.({ candidate: { ...candidate, toJSON: () => candidate } }))
      if (type === 'answer') this.connect()
    }

    async setRemoteDescription({ type, sdp }) {
      await tick()
      if (this.signalingState !== (type === 'offer' ? 'stable' : 'have-local-offer')) throw domError('InvalidStateError')
      const { pc, media } = JSON.parse(sdp)
      this.remote = instances.find((candidate) => candidate.id === pc)
      for (const kind of media.slice(this.transceivers.length)) this.transceiver(kind, 'recvonly')
      this.signalingState = type === 'offer' ? 'have-remote-offer' : 'stable'
      for (const transceiver of this.transceivers) this.ontrack?.({ track: transceiver.receiver.track })
      if (type === 'answer') this.connect()
    }

    addIceCandidate(candidate) {
      this.candidates.push(candidate)
      return Promise.resolve()
    }

    restartIce() {
      this.restarts += 1
      this.needed()
    }

    connect() {
      const far = this.remote
      if (far?.remote !== this || this.signalingState !== 'stable' || far.signalingState !== 'stable') return
      for (const pc of [this, far]) {
        if (pc.connectionState === 'connected') continue
        pc.connectionState = 'connected'
        pc.onconnectionstatechange?.({})
        pc.channel.readyState = 'open'
        pc.channel.onopen?.({})
      }
      this.flow()
    }

    // Each receiver hears the far side's matching sender, when it sends.
    flow() {
      const far = this.remote
      if (!far || this.connectionState !== 'connected') return
      for (const [from, to] of [[this, far], [far, this]]) {
        from.transceivers.forEach((transceiver, i) => {
          const sends = transceiver.direction === 'sendrecv' && transceiver.sender.track?.readyState === 'live'
          to.transceivers[i]?.receiver.track.setMuted(!sends)
        })
      }
    }

    close() {
      this.closed = true
      this.connectionState = 'closed'
    }
  }

  return { instances, FakePeerConnection }
}

function environment({ devices = fakeDevices(), rtc = fakeRtc(), audio = {} } = {}) {
  const document = fakeDocument()
  const contexts = []
  const layer = document.createElement('div')
  const timers = new Map()
  let clock = 1000
  let nextTimer = 0
  return {
    devices,
    rtc,
    contexts,
    layer,
    advance(ms) { clock += ms },
    fireTimers() { for (const callback of timers.values()) callback() },
    get timerCount() { return timers.size },
    deps: {
      mediaDevices: devices,
      RTCPeerConnectionCtor: rtc.FakePeerConnection,
      MediaStreamCtor: FakeStream,
      AudioContextCtor: audioContextClass(contexts, audio),
      createElement: (tag) => document.createElement(tag),
      createSurface: () => createCallTileLayer({ getContainer: () => layer }),
      now: () => clock,
      setInterval: (callback) => { timers.set(++nextTimer, callback); return nextTimer },
      clearInterval: (id) => timers.delete(id),
    },
  }
}

function recorder() {
  const log = []
  const listeners = new Map()
  let settleReady
  let settleResult
  const ready = new Promise((resolve, reject) => { settleReady = { resolve, reject } })
  const result = new Promise((resolve, reject) => { settleResult = { resolve, reject } })
  ready.catch(() => {})
  result.catch(() => {})
  return {
    log,
    ready,
    result,
    channel: {
      ready(value) { log.push(['ready', value]); settleReady.resolve(value) },
      event(name, value) {
        log.push([name, value])
        for (const listener of listeners.get(name) || []) listener(value)
      },
      result(value) { log.push(['result', value]); settleReady.resolve(undefined); settleResult.resolve(value) },
      error(error) { log.push(['failure', error]); settleReady.reject(error); settleResult.reject(error) },
    },
    on(name, listener) { listeners.set(name, [...(listeners.get(name) || []), listener]) },
    events(name) { return log.filter(([entry]) => entry === name).map(([, value]) => value) },
  }
}

function openCall(env, input = {}, limits = { max_peers: 8 }) {
  const session = recorder()
  const handle = createCallProvider(env.deps).open({ input, declaration: { version: 1, limits }, channel: session.channel })
  session.control = (action, value) => handle.control(action, value)
  return session
}

// Everything an app can observe is plain JSON.
function assertPlainJson(value, path = 'value') {
  if (value === null || ['string', 'boolean'].includes(typeof value)) return
  if (typeof value === 'number') return assert.ok(Number.isFinite(value), path)
  const prototype = Object.getPrototypeOf(value)
  assert.ok(prototype === Object.prototype || prototype === Array.prototype, `${path} is plain`)
  for (const [key, child] of Object.entries(value)) assertPlainJson(child, `${path}.${key}`)
}

function assertAppSawOnlyJson(session) {
  for (const [kind, value] of session.log) if (kind !== 'failure') assertPlainJson(value, kind)
}

function relay(from, fromName, to, toName) {
  from.on('signal', ({ peer, data }) => {
    if (peer === toName) setImmediate(() => to.control('signal', { peer: fromName, data: JSON.parse(JSON.stringify(data)) }))
  })
}

// Alice (impolite, she offers) and Bob (polite), connected and relaying.
async function connectedPair({ aliceInput = { audio: true, video: true }, aliceDevices } = {}) {
  const rtc = fakeRtc()
  const aliceEnv = environment({ rtc, devices: aliceDevices })
  const bobEnv = environment({ rtc })
  const alice = openCall(aliceEnv, aliceInput)
  const bob = openCall(bobEnv, { audio: true, video: true })
  await alice.ready
  await bob.ready
  relay(alice, 'alice', bob, 'bob')
  relay(bob, 'bob', alice, 'alice')
  alice.control('connect', { peer: 'bob', polite: false })
  bob.control('connect', { peer: 'alice', polite: true })
  await settle()
  const [alicePc, bobPc] = rtc.instances
  return { aliceEnv, bobEnv, alice, bob, alicePc, bobPc }
}

const lastPeer = (session) => session.events('peer').at(-1)
const painted = (env) => env.layer.children.map((tile) => ({ video: tile.children[0], style: tile.style }))

test('media.call checks its input before touching any device', () => {
  const env = environment()
  const provider = createCallProvider(env.deps)
  const channel = { ready() {}, event() {}, result() {}, error() {} }
  for (const input of [
    { camera: true }, { audio: 'yes' }, { iceServers: 'stun:x' }, { iceServers: [null] },
    { iceServers: Array.from({ length: 5 }, () => ({ urls: 'stun:x' })) },
    { iceServers: [{ urls: 'https://x' }] }, { iceServers: [{ urls: 'turn:x' }] },
  ]) {
    assert.throws(() => provider.open({ input, declaration: { version: 1 }, channel }),
      (error) => error.code === 'invalid_request', JSON.stringify(input))
  }
  assert.deepEqual(env.devices.requests, [])
  assert.equal(env.contexts.length, 0)
})

test('a listener joins without devices, and a missing half of the devices is reported', async () => {
  const listener = environment()
  const session = openCall(listener, { audio: false, video: false })
  assert.deepEqual(await session.ready, { audio: false, video: false, playback: 'running' })
  assert.deepEqual(listener.devices.requests, [])

  const noCamera = openCall(environment({ devices: fakeDevices({ video: 'NotAllowedError' }) }), { audio: true, video: true })
  assert.deepEqual(await noCamera.ready, { audio: true, video: false, playback: 'running', videoError: 'denied' })
  const noMicrophone = openCall(environment({ devices: fakeDevices({ audio: 'NotFoundError' }) }), { audio: true, video: true })
  assert.deepEqual(await noMicrophone.ready, { audio: false, video: true, playback: 'running', audioError: 'unavailable' })
  const nothing = openCall(environment({ devices: fakeDevices({ audio: 'NotAllowedError' }) }), { audio: true })
  await assert.rejects(nothing.ready, (error) => error.code === 'denied')
})

test('the impolite side offers, the polite side answers, and media flows both ways', async () => {
  const { aliceEnv, bobEnv, alice, bob, alicePc, bobPc } = await connectedPair()
  assert.equal(alicePc.offers, 1)
  assert.equal(bobPc.offers, 0, 'the polite side never offers')
  assert.deepEqual(lastPeer(alice), { peer: 'bob', state: 'connected', audio: true, video: true, screen: false })
  assert.deepEqual(lastPeer(bob), { peer: 'alice', state: 'connected', audio: true, video: true, screen: false })
  assert.ok(alicePc.candidates.length && bobPc.candidates.length, 'candidates were relayed')

  // A volume reaches the playing audio; tiles paint the far video and your own.
  bob.control('volume', { gains: { alice: 0.4 } })
  assert.equal(bobEnv.contexts[0].nodes.find((node) => node.gain).gain.value, 0.4)
  bob.control('tiles', { tiles: [{ peer: 'alice', x: 4, y: 5, width: 80, height: 60 }, { peer: 'self', x: 90, y: 5, width: 80, height: 60 }] })
  const [far, self] = painted(bobEnv)
  assert.equal(far.video.srcObject.getTracks()[0], bobPc.transceivers[1].receiver.track)
  assert.equal(far.video.style.objectFit, 'cover')
  assert.equal(far.style.left, '4px')
  assert.equal(self.video.style.transform, 'scaleX(-1)', 'your own camera is mirrored')

  aliceEnv.devices.granted[0].getAudioTracks()[0].level = 0.5
  bobPc.transceivers[0].receiver.track.level = 0.5
  bobEnv.fireTimers()
  assert.ok(bob.events('levels').at(-1).peers.alice > 0)
  assertAppSawOnlyJson(alice)
  assertAppSawOnlyJson(bob)
})

test('two impolite sides are reported instead of colliding', async () => {
  const rtc = fakeRtc()
  const a = openCall(environment({ rtc }))
  const b = openCall(environment({ rtc }))
  await a.ready
  await b.ready
  relay(a, 'a', b, 'b')
  relay(b, 'b', a, 'a')
  a.control('connect', { peer: 'b', polite: false })
  b.control('connect', { peer: 'a', polite: false })
  await settle()
  assert.equal(a.events('error').at(-1).code, 'invalid_request')
  assert.notEqual(lastPeer(a).state, 'connected')
})

test('muting and the camera switch what is sent without renegotiating', async () => {
  const { bobEnv, alice, bob, alicePc } = await connectedPair()
  bob.control('tiles', { tiles: [{ peer: 'alice', x: 0, y: 0, width: 80, height: 60 }] })
  assert.equal(bobEnv.layer.children.length, 1)

  alice.control('local', { video: false, audio: false })
  await settle(10)
  assert.deepEqual(alice.events('local').at(-1), { audio: false, video: false, screen: false })
  assert.deepEqual(lastPeer(bob), { peer: 'alice', state: 'connected', audio: false, video: false, screen: false })
  assert.equal(bobEnv.layer.children.length, 0, 'a camera turned off clears its tile')

  alice.control('local', { video: true, audio: true })
  await settle(10)
  assert.deepEqual(lastPeer(bob), { peer: 'alice', state: 'connected', audio: true, video: true, screen: false })
  assert.equal(alicePc.offers, 1, 'no renegotiation')
})

test('a listener turns a device on later, and the latest choice wins while asking', async () => {
  const { alice, bob, aliceEnv, alicePc } = await connectedPair({ aliceInput: { audio: false, video: false } })
  assert.equal(lastPeer(bob).audio, false)

  alice.control('local', { audio: true })
  alice.control('local', { audio: true })
  await settle(20)
  assert.equal(aliceEnv.devices.requests.length, 1, 'asked once')
  assert.ok(aliceEnv.devices.requests[0].audio && !aliceEnv.devices.requests[0].video)
  assert.deepEqual(alice.events('local').at(-1), { audio: true, video: false, screen: false })
  assert.equal(lastPeer(bob).audio, true)
  assert.equal(alicePc.offers, 1, 'no renegotiation')

  // Turned off again before the camera arrives: it arrives off.
  alice.control('local', { video: true })
  alice.control('local', { video: false })
  await settle(20)
  assert.equal(aliceEnv.devices.granted.at(-1).getVideoTracks()[0].enabled, false)
  assert.equal(lastPeer(bob).video, false)

  // A refusal is reported and the call carries on.
  const refused = await connectedPair({ aliceInput: { audio: false, video: false }, aliceDevices: fakeDevices({ audio: 'NotAllowedError' }) })
  refused.alice.control('local', { audio: true })
  await settle(20)
  assert.equal(refused.alice.events('error').at(-1).code, 'denied')
  assert.deepEqual(refused.alice.events('failure'), [])
})

test('a shared screen takes the camera\'s place and is letterboxed', async () => {
  const { aliceEnv, bobEnv, alice, bob, alicePc } = await connectedPair()
  bob.control('tiles', { tiles: [{ peer: 'alice', x: 0, y: 0, width: 320, height: 180 }] })
  alice.control('tiles', { tiles: [{ peer: 'self', x: 0, y: 0, width: 160, height: 90 }] })

  alice.control('screen', { share: true })
  await settle(20)
  assert.deepEqual(aliceEnv.devices.displayRequests, [{ video: { frameRate: { ideal: 15, max: 30 }, width: { max: 1920 }, height: { max: 1080 } }, audio: false }])
  const [screen] = aliceEnv.devices.displays[0].getVideoTracks()
  assert.equal(alicePc.transceivers[1].sender.track, screen)
  assert.equal(screen.contentHint, 'detail')
  assert.deepEqual(alice.events('local').at(-1), { audio: true, video: true, screen: true })
  assert.deepEqual(lastPeer(bob), { peer: 'alice', state: 'connected', audio: true, video: true, screen: true })
  assert.equal(painted(bobEnv)[0].video.style.objectFit, 'contain')
  const [preview] = painted(aliceEnv)
  assert.equal(preview.video.srcObject.getTracks()[0], screen)
  assert.equal(preview.video.style.transform, '', 'a screen preview is not mirrored')

  alice.control('screen', { share: true })
  assert.equal(aliceEnv.devices.displayRequests.length, 1, 'one screen at a time')

  // The browser's own stop button ends it, and the camera returns.
  screen.end()
  await settle(10)
  assert.equal(screen.stops, 1)
  assert.equal(alicePc.transceivers[1].sender.track, aliceEnv.devices.granted[0].getVideoTracks()[0])
  assert.equal(lastPeer(bob).screen, false)
  assert.equal(alicePc.offers, 1, 'no renegotiation')
})

test('a refused or late screen picker never ends the call or leaks a capture', async () => {
  const devices = fakeDevices({ display: 'NotAllowedError' })
  const session = openCall(environment({ devices }))
  await session.ready
  session.control('screen', { share: true })
  await settle(10)
  assert.equal(session.events('error').at(-1).code, 'denied')

  devices.plan.display = undefined
  devices.plan.deferDisplay = true
  session.control('screen', { share: true })
  session.control('finish')
  await session.result
  const late = new FakeTrack('video')
  devices.pickers[0].resolve(new FakeStream([late]))
  await settle(10)
  assert.equal(late.stops, 1)
})

test('limits, bad controls and an ICE failure never end the call', async () => {
  const crowded = openCall(environment(), {}, { max_peers: 1 })
  await crowded.ready
  crowded.control('connect', { peer: 'p1', polite: true })
  crowded.control('connect', { peer: 'p2', polite: true })
  assert.equal(crowded.events('error').at(-1).code, 'limit_exceeded')

  const { alice, alicePc } = await connectedPair()
  for (const [action, value] of [
    ['connect', { peer: 'self', polite: true }],
    ['tiles', { tiles: [{ peer: 'bob', x: 0, y: 0, width: -1, height: 1 }] }],
    ['local', { audio: 'on' }],
    ['signal', { peer: 'bob', data: { description: { type: 'pranswer', sdp: 'x' } } }],
    ['dance', {}],
  ]) {
    alice.control(action, value)
  }
  assert.equal(alice.events('error').filter((e) => e.code === 'invalid_request').length, 5)
  assert.deepEqual(alice.events('failure'), [])

  alicePc.iceConnectionState = 'failed'
  alicePc.oniceconnectionstatechange?.({})
  await settle(10)
  assert.equal(alicePc.restarts, 1)
  assert.equal(alicePc.offers, 2, 'the offering side restarts ICE')
})

test('finish releases every device, connection, tile and the audio context', async () => {
  const { aliceEnv, alice, alicePc } = await connectedPair()
  alice.control('tiles', { tiles: [{ peer: 'bob', x: 0, y: 0, width: 10, height: 10 }] })
  alice.control('screen', { share: true })
  await settle(10)
  aliceEnv.advance(4250)
  alice.control('finish')
  assert.deepEqual(await alice.result, { durationMs: 4250 })
  assert.ok(alicePc.closed)
  assert.ok(aliceEnv.devices.granted[0].getTracks().every((track) => track.stops === 1))
  assert.equal(aliceEnv.devices.displays[0].getTracks()[0].stops, 1)
  assert.equal(aliceEnv.layer.children.length, 0)
  assert.equal(aliceEnv.contexts[0].state, 'closed')
  assert.equal(aliceEnv.timerCount, 0)
  const logged = alice.log.length
  alice.control('finish')
  alice.control('connect', { peer: 'p3', polite: false })
  assert.equal(alice.log.length, logged, 'idempotent')
})

test('the host keeps one call per frame and ends it on deactivation and detach', async () => {
  const env = environment()
  const providers = builtInCapabilityProviders({ call: env.deps })
  const sent = []
  const source = { id: 'frame' }
  const host = createCapabilityHost({
    providers: { [MEDIA_CALL]: providers[MEDIA_CALL] },
    getDeclaration: () => ({ version: 1, lifecycle: 'active_frame', limits: { max_peers: 4 } }),
    isActive: () => true,
    send(_target, message) { sent.push(message) },
  })
  const message = (requestId, type, fields) => host.handle(source, { type, requestId, capability: MEDIA_CALL, ...fields })
  const of = (requestId, type) => sent.filter((m) => m.requestId === requestId && m.type === type)

  message('call-1', 'moebius:capability-open', { version: 1, input: { audio: true, video: true } })
  // Sent before readiness, it waits for the devices.
  message('call-1', 'moebius:capability-control', { action: 'connect', value: { peer: 'p1', polite: false } })
  await settle()
  assert.equal(of('call-1', 'moebius:capability-ready').length, 1)
  assert.equal(env.rtc.instances.length, 1)
  message('call-2', 'moebius:capability-open', { version: 1, input: {} })
  assert.equal(of('call-2', 'moebius:capability-error')[0].code, 'busy')

  host.deactivate()
  await settle(4)
  assert.equal(of('call-1', 'moebius:capability-error')[0].code, 'aborted')
  assert.ok(env.rtc.instances[0].closed)
  assert.equal(env.contexts[0].state, 'closed')

  message('call-3', 'moebius:capability-open', { version: 1, input: {} })
  await settle()
  host.detachSource(source)
  await settle(4)
  assert.equal(of('call-3', 'moebius:capability-error')[0].code, 'aborted')
  for (const entry of sent) assertPlainJson(entry.value ?? null, entry.type)
})

test('the tile layer reuses elements, stacks in list order, and clears on destroy', () => {
  const document = fakeDocument()
  const container = document.createElement('div')
  const layer = createCallTileLayer({ getContainer: () => container })
  const a = new FakeStream([new FakeTrack('video')])
  const b = new FakeStream([new FakeTrack('video')])
  const tile = (peer, stream, x) => ({ peer, stream, x, y: 0, width: 10, height: 10, radius: 2, opacity: 1, fit: 'cover', mirror: false })
  layer.paint([tile('p1', a, 0), tile('p2', b, 20)])
  const [first, second] = container.children
  layer.paint([tile('p1', a, 5), tile('p2', b, 20), tile('p1', a, 40)])
  assert.equal(container.children[0], first, 'moved in place')
  assert.equal(first.style.left, '5px')
  assert.equal(container.children[1], second)
  assert.equal(container.children.length, 3, 'a person can be painted twice')
  assert.equal(container.children[2].style.zIndex, '3')
  layer.paint([tile('p2', b, 20)])
  assert.equal(container.children.length, 1)
  layer.destroy()
  assert.equal(container.children.length, 0)
})
