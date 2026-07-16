const DEFAULT_MAX_SECONDS = 30
const MAX_SECONDS = 60

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
  maxSeconds,
  onLevel,
} = {}) {
  if (!mediaDevices?.getUserMedia) {
    throw new Error('Microphone recording is unavailable in this browser.')
  }
  if (!AudioContextCtor) {
    throw new Error('Audio recording is unavailable in this browser.')
  }

  const stream = await mediaDevices.getUserMedia({
    audio: {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  })

  let context
  let source
  let processor
  let silent
  let timer
  let settled = false
  let resolveDone
  let rejectDone
  const chunks = []
  let sampleCount = 0

  const done = new Promise((resolve, reject) => {
    resolveDone = resolve
    rejectDone = reject
  })

  function cleanup() {
    clearTimeout(timer)
    if (processor) processor.onaudioprocess = null
    cleanupNode(processor)
    cleanupNode(silent)
    cleanupNode(source)
    stream.getTracks().forEach((track) => track.stop())
    try { context?.close?.() } catch {}
  }

  function finish(cancelled = false) {
    if (settled) return done
    settled = true
    cleanup()
    if (cancelled) {
      const error = new Error('Recording cancelled.')
      error.name = 'AbortError'
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
    context = new AudioContextCtor()
    if (context.state === 'suspended') await context.resume?.()
    source = context.createMediaStreamSource(stream)
    processor = context.createScriptProcessor(4096, 1, 1)
    silent = context.createGain()
    silent.gain.value = 0
    source.connect(processor)
    processor.connect(silent)
    silent.connect(context.destination)

    const seconds = normalizeMicrophoneSeconds(maxSeconds)
    const maxFrames = Math.max(1, Math.round(context.sampleRate * seconds))
    processor.onaudioprocess = (event) => {
      if (settled) return
      const input = event.inputBuffer.getChannelData(0)
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
    timer = setTimeout(() => finish(false), seconds * 1000 + 100)
  } catch (error) {
    cleanup()
    settled = true
    rejectDone(error)
    // Avoid an unhandled rejection when setup itself fails before a caller can
    // receive the returned control object.
    done.catch(() => {})
    throw error
  }

  return {
    sampleRate: context.sampleRate,
    done,
    stop: () => finish(false),
    cancel: () => finish(true),
  }
}
