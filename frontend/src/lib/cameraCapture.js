const DEFAULT_MAX_DURATION_MS = 60_000
const DEFAULT_MAX_BYTES = 128 * 1024 * 1024
const PROGRESS_INTERVAL_MS = 500

function namedError(name, message, code = null) {
  const error = new Error(message)
  error.name = name
  if (code) error.code = code
  return error
}

function finitePositive(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

function stopTracks(stream, stoppedTracks) {
  for (const track of stream?.getTracks?.() || []) {
    if (stoppedTracks.has(track)) continue
    stoppedTracks.add(track)
    try { track.stop() } catch {}
  }
}

/**
 * Record one bounded video in the trusted top-level shell.
 *
 * The returned control object exists before getUserMedia settles so frame
 * teardown can cancel a pending permission/device startup. A browser prompt
 * itself is browser-owned and cannot be dismissed programmatically; if it
 * later resolves, its tracks are stopped without starting a recorder.
 */
export function startCameraCapture({
  mediaDevices = globalThis.navigator?.mediaDevices,
  MediaRecorderCtor = globalThis.MediaRecorder,
  BlobCtor = globalThis.Blob,
  facingMode = 'environment',
  audio = false,
  maxDurationMs = DEFAULT_MAX_DURATION_MS,
  maxBytes = DEFAULT_MAX_BYTES,
  onProgress,
  onPreviewStream,
  now = () => globalThis.performance?.now?.() ?? Date.now(),
} = {}) {
  if (!mediaDevices?.getUserMedia || !MediaRecorderCtor || !BlobCtor) {
    throw namedError(
      'NotSupportedError',
      'Video recording is unavailable in this browser.',
    )
  }

  const durationLimit = finitePositive(maxDurationMs, DEFAULT_MAX_DURATION_MS)
  const byteLimit = Math.floor(finitePositive(maxBytes, DEFAULT_MAX_BYTES))
  const stoppedTracks = new Set()
  const chunks = []
  let stream = null
  let recorder = null
  let durationTimer = null
  let startedAt = null
  let recordedBytes = 0
  let previewStream = null
  let settled = false
  let stopping = false
  let readySettled = false
  let resolveReady
  let rejectReady
  let resolveDone
  let rejectDone
  let metadata = null

  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve
    rejectReady = reject
  })
  const done = new Promise((resolve, reject) => {
    resolveDone = resolve
    rejectDone = reject
  })
  // Cancellation and setup failure can happen before a provider attaches its
  // chain. Keep both promises observed during that short startup window.
  ready.catch(() => {})
  done.catch(() => {})

  function elapsedMs() {
    if (startedAt == null) return 0
    return Math.max(0, Math.min(durationLimit, Math.round(now() - startedAt)))
  }

  function emitProgress() {
    if (typeof onProgress !== 'function') return
    try {
      onProgress({ durationMs: elapsedMs(), bytes: recordedBytes })
    } catch {}
  }

  function settleReady(value, error = null) {
    if (readySettled) return
    readySettled = true
    if (error) rejectReady(error)
    else resolveReady(value)
  }

  function publishPreview(nextStream) {
    if (previewStream === nextStream) return
    previewStream = nextStream
    if (typeof onPreviewStream !== 'function') return
    try { onPreviewStream(nextStream) } catch {}
  }

  function release() {
    clearTimeout(durationTimer)
    durationTimer = null
    publishPreview(null)
    stopTracks(stream, stoppedTracks)
  }

  function fail(error, { stopRecorder = true } = {}) {
    if (settled) return done
    settled = true
    clearTimeout(durationTimer)
    durationTimer = null
    if (stopRecorder && recorder?.state && recorder.state !== 'inactive') {
      try { recorder.stop() } catch {}
    }
    release()
    settleReady(null, error)
    rejectDone(error)
    return done
  }

  async function complete() {
    if (settled) return
    release()
    try {
      const mimeType = recorder?.mimeType
        || chunks.find((chunk) => chunk?.type)?.type
        || 'application/octet-stream'
      const bytes = await new BlobCtor(chunks, { type: mimeType }).arrayBuffer()
      if (settled) return
      if (bytes.byteLength === 0) {
        fail(namedError(
          'NotReadableError',
          'The camera stopped without producing video. Check the camera, then try again.',
        ), { stopRecorder: false })
        return
      }
      if (bytes.byteLength > byteLimit) {
        fail(namedError(
          'QuotaExceededError',
          'The recording exceeded this app\'s reviewed video size limit.',
          'limit_exceeded',
        ), { stopRecorder: false })
        return
      }
      settled = true
      settleReady(metadata)
      resolveDone({
        mimeType,
        durationMs: elapsedMs(),
        width: metadata.width,
        height: metadata.height,
        bytes,
      })
    } catch (error) {
      fail(error, { stopRecorder: false })
    }
  }

  function finish() {
    if (settled) return done
    if (stopping) return done
    if (!readySettled || !recorder || recorder.state === 'inactive') {
      return fail(namedError(
        'NotReadableError',
        'The camera did not become ready before recording stopped. Check camera permissions, then try again.',
      ))
    }
    clearTimeout(durationTimer)
    durationTimer = null
    stopping = true
    try {
      recorder.stop()
    } catch (error) {
      fail(error, { stopRecorder: false })
    }
    return done
  }

  function cancel() {
    return fail(namedError('AbortError', 'Video recording cancelled.'))
  }

  Promise.resolve().then(async () => {
    if (settled) return
    let acquired = null
    try {
      acquired = await mediaDevices.getUserMedia({
        video: { facingMode: { ideal: facingMode } },
        audio,
      })
      if (settled) {
        stopTracks(acquired, stoppedTracks)
        return
      }
      stream = acquired
      const videoTrack = stream.getVideoTracks?.()[0]
      if (!videoTrack) {
        throw namedError('NotFoundError', 'No camera video track was available.')
      }
      const settings = videoTrack.getSettings?.() || {}
      recorder = new MediaRecorderCtor(stream)
      metadata = {
        mimeType: recorder.mimeType || null,
        width: Math.max(0, Math.round(Number(settings.width) || 0)),
        height: Math.max(0, Math.round(Number(settings.height) || 0)),
        audio,
      }
      recorder.ondataavailable = (event) => {
        if (settled || !event?.data || event.data.size <= 0) return
        if (recordedBytes + event.data.size > byteLimit) {
          fail(namedError(
            'QuotaExceededError',
            'The recording exceeded this app\'s reviewed video size limit.',
            'limit_exceeded',
          ))
          return
        }
        chunks.push(event.data)
        recordedBytes += event.data.size
        emitProgress()
      }
      recorder.onerror = (event) => {
        fail(event?.error || namedError(
          'NotReadableError',
          'The camera stopped recording unexpectedly. Try again.',
        ))
      }
      recorder.onstop = () => { void complete() }
      publishPreview(stream)
      recorder.start(PROGRESS_INTERVAL_MS)
      metadata.mimeType = recorder.mimeType || metadata.mimeType
      startedAt = now()
      settleReady(metadata)
      emitProgress()
      durationTimer = setTimeout(finish, durationLimit)
    } catch (error) {
      if (acquired && acquired !== stream) stopTracks(acquired, stoppedTracks)
      fail(error)
    }
  })

  return {
    ready,
    done,
    stop: finish,
    cancel,
  }
}
