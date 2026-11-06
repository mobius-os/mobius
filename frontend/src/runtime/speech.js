import { normalizeSpeechInput } from '../lib/speech/speechDocument.js'
import { SPEECH_WORKER_URL, SPEECH_WASM_URL } from '../lib/speech/speechWorkerAsset.js'

// Speech runs in the frame that asked for it, not in the shell.
//
// A document's Content-Security-Policy governs every worker it spawns. App
// frames are granted WebAssembly; the shell is a stricter document whose policy
// also has to travel through a service-worker precache to change. Synthesising
// here means the engine only ever depends on the policy of the frame it runs
// in, which is the one policy the caller can be sure of.
//
// The shell still owns the model: it holds the download, and streams those
// bytes over the same capability request. So there is one engine, one copy of
// the model, and no app-side engine code.

const LOAD_TIMEOUT_MS = 180_000

// A loaded engine outlives the session that loaded it. Starting one fetches the
// worker, streams the whole model from the shell and starts WebAssembly; a
// reader that resumes, seeks or asks for speech in several requests would pay
// that cold start every time. The frame keeps at most one idle engine, for the
// exact model and voice it holds, and only while the app is on screen: a
// hidden app that is not speaking gives the model's memory back.
let idleEngine = null
let retainIdleEngine = true
let nextRequestId = 1

function retireEngine(engine) {
  if (!engine) return
  engine.worker.onmessage = null
  engine.worker.onerror = null
  try { engine.worker.terminate() } catch { /* already gone */ }
  if (engine.blobUrl) URL.revokeObjectURL(engine.blobUrl)
}

export function releaseSpeechEngine() {
  const engine = idleEngine
  idleEngine = null
  retireEngine(engine)
}

export function retainSpeechEngine(retain) {
  retainIdleEngine = retain !== false
  if (!retainIdleEngine) releaseSpeechEngine()
}

function parkEngine(engine) {
  if (!retainIdleEngine || idleEngine) {
    retireEngine(engine)
    return
  }
  engine.worker.onmessage = null
  engine.worker.onerror = () => { if (idleEngine === engine) releaseSpeechEngine() }
  idleEngine = engine
}

function sameVoice(left, right) {
  if (left === right) return true
  if (!left || !right || left.length !== right.length) return false
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) return false
  }
  return true
}

// What a loaded worker holds: the model the shell streamed and the voice it
// was cloned from. A re-recorded clone keeps its model id, so the voice
// samples themselves are compared.
function engineKey(input, manifest) {
  const voice = input?.clonedVoiceSamples || manifest.clonedVoiceSamples
  return {
    model: JSON.stringify([
      input?.modelId || '', input?.engineId || '', manifest.temperature ?? null, manifest.assetBytes,
    ]),
    voice: voice instanceof Float32Array ? voice.slice() : null,
  }
}

function takeIdleEngine(key) {
  const engine = idleEngine
  if (engine && engine.key.model === key.model && sameVoice(engine.key.voice, key.voice)) {
    idleEngine = null
    return engine
  }
  // A different model: free the old one before the new one is allocated.
  releaseSpeechEngine()
  return null
}

function speechError(code, message, name = 'CapabilityError') {
  const error = new Error(message)
  error.code = code
  error.name = name
  return error
}

/**
 * Drive the shared speech worker inside this frame.
 *
 * `openModelStream` opens the shell-side request that supplies model bytes; it
 * receives handlers for the manifest and each chunk, and returns a session whose
 * `control('start')` releases the stream once the engine has allocated. When an
 * idle engine already holds the manifest's model, the stream is cancelled
 * before any bytes move and generation starts at once.
 */
export function synthesizeInFrame({ input, channel, openModelStream, maxTextChars }) {
  const document = normalizeSpeechInput(
    { text: input?.text, document: input?.document },
    maxTextChars,
  )

  let engine = null
  let loaded = false
  let stream = null
  let finished = false
  let startupTimer = null
  let requestId = ''
  let ready = Promise.resolve()
  let settleResult
  let failResult
  const result = new Promise((resolve, reject) => { settleResult = resolve; failResult = reject })
  result.catch(() => {})

  const clearStartupTimeout = () => {
    if (startupTimer === null) return
    clearTimeout(startupTimer)
    startupTimer = null
  }

  // Loading a model is a streaming operation. A single wall-clock deadline
  // falsely fails a healthy load on slower devices, while no deadline leaves
  // a genuinely stalled worker hanging forever. Reset this inactivity
  // watchdog only when one side of the startup handshake makes real progress.
  const armStartupTimeout = () => {
    if (finished || loaded) return
    clearStartupTimeout()
    startupTimer = setTimeout(
      () => fail(speechError('timeout', 'The speech engine stopped making progress while starting.', 'TimeoutError')),
      LOAD_TIMEOUT_MS,
    )
  }

  const closeStream = () => {
    const open = stream
    stream = null
    try { open?.cancel?.() } catch { /* already closed */ }
  }

  // A loaded engine goes back to the frame for the next session; one that
  // never finished loading, or failed, is discarded.
  const finish = (keepEngine) => {
    finished = true
    clearStartupTimeout()
    closeStream()
    const used = engine
    engine = null
    if (!used) return
    if (keepEngine && loaded) parkEngine(used)
    else retireEngine(used)
  }

  const fail = (error) => {
    if (finished) return
    finish(false)
    failResult(error)
  }

  const succeed = (value) => {
    if (finished) return
    finish(true)
    settleResult(value)
  }

  armStartupTimeout()

  // Messages must wait for the fetched worker script to actually load: a
  // message to a worker that does not exist yet is silently dropped and the
  // engine then hangs forever at "starting". `ready` resolves once the worker
  // is constructed; every send chains behind it.
  let queue = Promise.resolve()
  const send = (message, transfer = []) => {
    queue = queue.then(() => ready).then(() => {
      if (!finished && engine) engine.worker.postMessage(message, transfer)
    })
    queue.catch(() => {})
  }

  let segmentIndex = 0
  const generateNext = () => {
    if (segmentIndex >= document.segments.length) {
      succeed({ segmentCount: document.segments.length })
      return
    }
    const segment = document.segments[segmentIndex]
    channel.event('loading', {
      stage: 'generating',
      percent: Math.round(segmentIndex / document.segments.length * 100),
      segmentIndex,
      segmentCount: document.segments.length,
    })
    requestId = `frame-${nextRequestId++}`
    send({ type: 'generate', requestId, text: segment.text })
  }

  const startGenerating = () => {
    loaded = true
    clearStartupTimeout()
    channel.event('loading', { stage: 'preparing', percent: 100 })
    generateNext()
  }

  const onWorkerMessage = (data) => {
    if (!data || finished) return
    // A reused engine may still be unwinding a cancelled session's request.
    if (data.requestId && data.requestId !== requestId) return
    if (data.type === 'load-ready') {
      armStartupTimeout()
      // The engine has compiled and reserved the model's buffer; only now may
      // the bytes flow, so they are never held twice.
      stream?.control?.('start')
      return
    }
    if (data.type === 'chunk-accepted') {
      armStartupTimeout()
      stream?.control?.('chunk-accepted')
      return
    }
    if (data.type === 'load-complete') {
      // The startup watchdog ends here. Long reports can legitimately spend
      // many minutes generating (especially while playback is paused), so
      // keeping it armed turns a healthy resumed player into a false
      // "engine took too long to start" failure.
      startGenerating()
      return
    }
    if (data.type === 'audio') {
      channel.event('audio', { samples: data.samples }, data.samples?.buffer ? [data.samples.buffer] : [])
      return
    }
    if (data.type === 'generate-complete') {
      const segment = document.segments[segmentIndex]
      segmentIndex += 1
      // One boundary per segment the caller sent. A long segment is generated
      // in several pieces, but the caller only knows its own segments.
      if (document.segments[segmentIndex]?.sourceIndex !== segment.sourceIndex) {
        channel.event('boundary', {
          segmentIndex: segment.sourceIndex,
          kind: segment.kind,
          pauseAfterMs: segment.pauseAfterMs,
        })
      }
      generateNext()
      return
    }
    if (data.type === 'worker-error' || data.type === 'generate-error') {
      fail(speechError('engine_failed', data.error?.message || 'Speech stopped unexpectedly.'))
    }
  }

  const bindEngine = (value) => {
    engine = value
    engine.worker.onerror = (event) => fail(speechError(
      'engine_blocked',
      event?.message || `The speech engine could not start: ${SPEECH_WORKER_URL} did not load.`,
    ))
    engine.worker.onmessage = ({ data }) => onWorkerMessage(data)
  }

  const startEngine = async (key) => {
    // An opaque app frame cannot construct a Worker from a URL — that URL is
    // never same-origin with an opaque document — so the script is fetched and
    // wrapped in a Blob, which the frame's policy permits. A blob: worker has
    // no base to resolve against, so the binary is passed as an absolute URL.
    const response = await fetch(SPEECH_WORKER_URL, { credentials: 'same-origin' })
    if (!response.ok) {
      throw speechError('engine_missing', `The speech engine script returned ${response.status}.`)
    }
    const source = await response.text()
    if (finished) return
    const blobUrl = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
    bindEngine({ key, worker: new Worker(blobUrl), blobUrl })
    armStartupTimeout()
  }

  channel.event('loading', { stage: 'starting', percent: 0 })
  stream = openModelStream({
    modelId: input?.modelId,
    engineId: input?.engineId,
    onManifest: (value) => {
      armStartupTimeout()
      if (finished) return
      const key = engineKey(input, value)
      const warm = takeIdleEngine(key)
      if (warm) {
        bindEngine(warm)
        closeStream()
        startGenerating()
        return
      }
      ready = startEngine(key)
      ready.catch(fail)
      send({
        type: 'load-start',
        wasmUrl: new URL(SPEECH_WASM_URL, globalThis.location.href).href,
        assetBytes: value.assetBytes,
        temperature: value.temperature,
        clonedVoiceSamples: input?.clonedVoiceSamples || value.clonedVoiceSamples || undefined,
      })
    },
    onChunk: (value) => {
      armStartupTimeout()
      send({
        type: 'asset-chunk',
        chunkId: value.index,
        assetId: value.assetId,
        index: value.index,
        offset: value.offset,
        bytes: value.bytes,
      }, [value.bytes])
    },
    onProgress: (value) => {
      armStartupTimeout()
      channel.event('loading', { stage: 'reading', ...value })
    },
    onComplete: () => {
      armStartupTimeout()
      send({ type: 'load-finish' })
    },
    // A stream this session closed itself (to reuse an engine) reports an
    // abort that is not a failure.
    onError: (error) => { if (stream) fail(error) },
  })

  return {
    result,
    cancel() {
      if (finished) return
      // Stop the current generation; the loaded engine stays for the next
      // session, which ignores whatever this request still emits.
      if (loaded) engine?.worker.postMessage({ type: 'cancel-generate' })
      finish(true)
      failResult(speechError('aborted', 'Speech stopped.', 'AbortError'))
    },
  }
}
