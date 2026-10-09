import { acquireAudioSession } from './audioSession.js'

const DEFAULT_MAX_SECONDS = 30
const MAX_SECONDS = 60
const START_TIMEOUT_MS = 5_000

export function normalizeMicrophoneSeconds(value) {
  const seconds = Number(value)
  if (!Number.isFinite(seconds)) return DEFAULT_MAX_SECONDS
  return Math.max(0.1, Math.min(MAX_SECONDS, seconds))
}

function cleanupNode(node) {
  try { node?.disconnect?.() } catch {}
}

/**
 * Capture mono PCM in the trusted top-level shell.
 *
 * Sandboxed mini-app frames intentionally have an opaque origin, so browsers
 * reject getUserMedia() inside them even when the iframe has a microphone
 * Permissions-Policy delegation. AppCanvas uses this helper and transfers only
 * the resulting Float32Array back to the exact frame that requested it.
 */
export async function startMicrophoneCapture({
  mediaDevices = globalThis.navigator?.mediaDevices,
  AudioContextCtor = globalThis.AudioContext || globalThis.webkitAudioContext,
  navigator = globalThis.navigator,
  maxSeconds,
  onLevel,
} = {}) {
  if (!mediaDevices?.getUserMedia) {
    throw new Error('Microphone recording is unavailable in this browser.')
  }
  if (!AudioContextCtor) {
    throw new Error('Audio recording is unavailable in this browser.')
  }

  const releaseAudioSession = acquireAudioSession('play-and-record', navigator)
  let stream
  let context
  let source
  let processor
  let silent
  let recordingTimer
  let startTimer
  let settled = false
  let resolveDone
  let rejectDone
  let resolveReady
  let rejectReady
  let readySettled = false
  const chunks = []
  let sampleCount = 0

  const done = new Promise((resolve, reject) => {
    resolveDone = resolve
    rejectDone = reject
  })
  // A capture may be cancelled or fail before the first audio callback. Keep
  // the promise observable to callers without producing a global unhandled
  // rejection in that setup window.
  done.catch(() => {})
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve
    rejectReady = reject
  })
  ready.catch(() => {})

  function settleReady(error = null) {
    if (readySettled) return
    readySettled = true
    clearTimeout(startTimer)
    if (error) rejectReady(error)
    else resolveReady({ sampleRate: context.sampleRate })
  }

  function cleanup() {
    clearTimeout(recordingTimer)
    clearTimeout(startTimer)
    if (processor) processor.onaudioprocess = null
    cleanupNode(processor)
    cleanupNode(silent)
    cleanupNode(source)
    stream?.getTracks?.().forEach((track) => track.stop())
    try { context?.close?.() } catch {}
    releaseAudioSession()
  }

  function finish(cancelled = false) {
    if (settled) return done
    settled = true
    cleanup()
    if (cancelled) {
      const error = new Error('Recording cancelled.')
      error.name = 'AbortError'
      settleReady(error)
      rejectDone(error)
      return done
    }

    if (!readySettled) {
      const error = new Error('The microphone did not start delivering audio. Check your microphone permissions and input, then try again.')
      error.name = 'NotReadableError'
      settleReady(error)
      rejectDone(error)
      return done
    }

    const samples = new Float32Array(sampleCount)
    let offset = 0
    for (const chunk of chunks) {
      samples.set(chunk, offset)
      offset += chunk.length
    }
    resolveDone({ samples, sampleRate: context.sampleRate })
    return done
  }

  try {
    // Construct and resume this while the originating click can still carry
    // user activation. A microphone permission prompt may consume that
    // activation; creating the context after it returns can leave the graph
    // permanently suspended and therefore unable to deliver PCM callbacks.
    context = new AudioContextCtor()
    if (context.state === 'suspended') Promise.resolve(context.resume?.()).catch(() => {})
    stream = await mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    })
    source = context.createMediaStreamSource(stream)
    processor = context.createScriptProcessor(4096, 1, 1)
    silent = context.createGain()
    silent.gain.value = 0

    const seconds = normalizeMicrophoneSeconds(maxSeconds)
    const maxFrames = Math.max(1, Math.round(context.sampleRate * seconds))
    processor.onaudioprocess = (event) => {
      if (settled) return
      const input = event.inputBuffer.getChannelData(0)
      if (!readySettled) {
        settleReady()
        recordingTimer = setTimeout(() => finish(false), seconds * 1000 + 100)
      }
      const remaining = maxFrames - sampleCount
      if (remaining <= 0) {
        finish(false)
        return
      }
      const chunk = new Float32Array(input.subarray(0, Math.min(input.length, remaining)))
      chunks.push(chunk)
      sampleCount += chunk.length

      if (typeof onLevel === 'function') {
        let peak = 0
        for (let i = 0; i < chunk.length; i += 1) peak = Math.max(peak, Math.abs(chunk[i]))
        try { onLevel(Math.min(1, peak)) } catch {}
      }
      if (sampleCount >= maxFrames) finish(false)
    }
    startTimer = setTimeout(() => finish(false), START_TIMEOUT_MS)
    source.connect(processor)
    processor.connect(silent)
    silent.connect(context.destination)
  } catch (error) {
    cleanup()
    settled = true
    settleReady(error)
    rejectDone(error)
    throw error
  }

  return {
    sampleRate: context.sampleRate,
    ready,
    done,
    stop: () => finish(false),
    cancel: () => finish(true),
  }
}
