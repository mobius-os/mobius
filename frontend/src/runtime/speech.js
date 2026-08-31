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
 * `control('start')` releases the stream once the engine has allocated.
 */
export function synthesizeInFrame({ input, channel, openModelStream, maxTextChars }) {
  const document = normalizeSpeechInput(
    { text: input?.text, document: input?.document },
    maxTextChars,
  )

  let worker = null
  let blobUrl = ''
  let stream = null
  let finished = false
  let startupComplete = false
  let startupTimer = null
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
    if (finished || startupComplete) return
    clearStartupTimeout()
    startupTimer = setTimeout(
      () => fail(speechError('timeout', 'The speech engine stopped making progress while starting.', 'TimeoutError')),
      LOAD_TIMEOUT_MS,
    )
  }

  const cleanup = () => {
    try { stream?.cancel?.() } catch { /* already closed */ }
    try { worker?.terminate() } catch { /* already gone */ }
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = '' }
  }

  const fail = (error) => {
    if (finished) return
    finished = true
    clearStartupTimeout()
    cleanup()
    failResult(error)
  }

  const succeed = (value) => {
    if (finished) return
    finished = true
    clearStartupTimeout()
    cleanup()
    settleResult(value)
  }

  armStartupTimeout()

  // Messages must wait for the fetched worker script to actually load: the
  // model manifest can arrive (a single postMessage round-trip) before the
  // worker exists, and delivering `load-start` to a null worker silently drops
  // it — the engine then hangs forever at "starting". `ready` (below) resolves
  // once the worker is constructed; every send chains behind it.
  let queue = Promise.resolve()
  const send = (message, transfer = []) => {
    queue = queue.then(() => ready).then(() => {
      if (!finished && worker) worker.postMessage(message, transfer)
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
    send({ type: 'generate', requestId: `frame-${segmentIndex}`, text: segment.text })
  }

  const onWorkerMessage = (data) => {
    if (!data || finished) return
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
      // This watchdog covers startup only. Long reports can legitimately spend
      // many minutes generating (especially while playback is paused), so
      // keeping it armed here turns a healthy resumed player into a false
      // "engine took too long to start" failure.
      startupComplete = true
      clearStartupTimeout()
      channel.event('loading', { stage: 'preparing', percent: 100 })
      generateNext()
      return
    }
    if (data.type === 'audio') {
      channel.event('audio', { samples: data.samples }, data.samples?.buffer ? [data.samples.buffer] : [])
      return
    }
    if (data.type === 'generate-complete') {
      const segment = document.segments[segmentIndex]
      channel.event('boundary', {
        segmentIndex,
        kind: segment.kind,
        pauseAfterMs: segment.pauseAfterMs,
      })
      segmentIndex += 1
      generateNext()
      return
    }
    if (data.type === 'worker-error' || data.type === 'generate-error') {
      fail(speechError('engine_failed', data.error?.message || 'Speech stopped unexpectedly.'))
    }
  }

  const ready = (async () => {
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
    blobUrl = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
    worker = new Worker(blobUrl)
    armStartupTimeout()
    worker.onerror = (event) => fail(speechError(
      'engine_blocked',
      event?.message || `The speech engine could not start: ${SPEECH_WORKER_URL} did not load.`,
    ))
    worker.onmessage = ({ data }) => onWorkerMessage(data)
  })()
  ready.catch(fail)

  channel.event('loading', { stage: 'starting', percent: 0 })
  stream = openModelStream({
    modelId: input?.modelId,
    engineId: input?.engineId,
    onManifest: (value) => {
      armStartupTimeout()
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
    onError: fail,
  })

  return {
    result,
    cancel() {
      if (finished) return
      finished = true
      clearStartupTimeout()
      cleanup()
      failResult(speechError('aborted', 'Speech stopped.', 'AbortError'))
    },
  }
}
