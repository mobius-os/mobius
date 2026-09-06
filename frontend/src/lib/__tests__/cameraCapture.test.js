import { test } from 'node:test'
import assert from 'node:assert/strict'

import { startCameraCapture } from '../cameraCapture.js'

function videoStream({ width = 1920, height = 1080 } = {}) {
  let stops = 0
  const track = {
    getSettings: () => ({ width, height }),
    stop() { stops += 1 },
  }
  return {
    getTracks: () => [track],
    getVideoTracks: () => [track],
    get stops() { return stops },
  }
}

function recorderHarness({ mimeType = 'video/webm;codecs=vp8' } = {}) {
  let current
  class FakeMediaRecorder {
    constructor(stream) {
      this.stream = stream
      this.mimeType = mimeType
      this.state = 'inactive'
      current = this
    }
    start(timeslice) {
      this.timeslice = timeslice
      this.state = 'recording'
    }
    emit(bytes) {
      this.ondataavailable?.({ data: new Blob([Uint8Array.from(bytes)], { type: mimeType }) })
    }
    stop() {
      if (this.state === 'inactive') return
      this.state = 'inactive'
      this.onstop?.()
    }
  }
  return {
    FakeMediaRecorder,
    get recorder() { return current },
  }
}

test('shell camera capture records bounded bytes and releases its tracks', async () => {
  const stream = videoStream({ width: 1440, height: 1080 })
  const recorder = recorderHarness({ mimeType: 'video/webm' })
  const requests = []
  const progress = []
  const previews = []
  let clock = 100
  const capture = startCameraCapture({
    mediaDevices: {
      async getUserMedia(constraints) {
        requests.push(constraints)
        return stream
      },
    },
    MediaRecorderCtor: recorder.FakeMediaRecorder,
    facingMode: 'environment',
    audio: false,
    maxDurationMs: 8_000,
    maxBytes: 1024,
    now: () => clock,
    onProgress: (value) => progress.push(value),
    onPreviewStream: (value) => previews.push(value),
  })

  assert.deepEqual(await capture.ready, {
    mimeType: 'video/webm', width: 1440, height: 1080, audio: false,
  })
  assert.deepEqual(requests, [{
    video: { facingMode: { ideal: 'environment' } }, audio: false,
  }])
  assert.equal(recorder.recorder.timeslice, 500)
  assert.deepEqual(previews, [stream])

  clock = 350
  recorder.recorder.emit([1, 2, 3, 4])
  const resultPromise = capture.stop()
  const result = await resultPromise

  assert.equal(result.mimeType, 'video/webm')
  assert.equal(result.durationMs, 250)
  assert.equal(result.width, 1440)
  assert.equal(result.height, 1080)
  assert.deepEqual([...new Uint8Array(result.bytes)], [1, 2, 3, 4])
  assert.deepEqual(progress, [
    { durationMs: 0, bytes: 0 },
    { durationMs: 250, bytes: 4 },
  ])
  assert.equal(stream.stops, 1)
  assert.deepEqual(previews, [stream, null])

  assert.equal(await capture.stop(), result)
  assert.equal(stream.stops, 1)
})

test('camera cancellation settles while permission is pending and stops late tracks', async () => {
  let resolveStream
  let requests = 0
  const stream = videoStream()
  const recorder = recorderHarness()
  const capture = startCameraCapture({
    mediaDevices: {
      getUserMedia() {
        requests += 1
        return new Promise((resolve) => { resolveStream = resolve })
      },
    },
    MediaRecorderCtor: recorder.FakeMediaRecorder,
  })
  await Promise.resolve()
  assert.equal(requests, 1)

  const done = capture.cancel()
  await assert.rejects(capture.ready, { name: 'AbortError' })
  await assert.rejects(done, { name: 'AbortError' })
  resolveStream(stream)
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(stream.stops, 1)
  assert.equal(recorder.recorder, undefined)
  await assert.rejects(capture.cancel(), { name: 'AbortError' })
  assert.equal(stream.stops, 1)
})

test('camera finish is safe before startup and does not open a permission prompt', async () => {
  let requests = 0
  const recorder = recorderHarness()
  const capture = startCameraCapture({
    mediaDevices: {
      async getUserMedia() {
        requests += 1
        return videoStream()
      },
    },
    MediaRecorderCtor: recorder.FakeMediaRecorder,
  })

  await assert.rejects(capture.stop(), { name: 'NotReadableError' })
  await assert.rejects(capture.ready, { name: 'NotReadableError' })
  await Promise.resolve()
  assert.equal(requests, 0)
})

test('camera capture fails closed at the reviewed byte ceiling', async () => {
  const stream = videoStream()
  const recorder = recorderHarness()
  const capture = startCameraCapture({
    mediaDevices: { async getUserMedia() { return stream } },
    MediaRecorderCtor: recorder.FakeMediaRecorder,
    maxBytes: 3,
  })
  await capture.ready

  recorder.recorder.emit([1, 2, 3, 4])

  await assert.rejects(capture.done, {
    name: 'QuotaExceededError', code: 'limit_exceeded',
  })
  assert.equal(stream.stops, 1)
  assert.equal(recorder.recorder.state, 'inactive')
})

test('camera capture never presents an empty recording as useful video', async () => {
  const stream = videoStream()
  const recorder = recorderHarness()
  const capture = startCameraCapture({
    mediaDevices: { async getUserMedia() { return stream } },
    MediaRecorderCtor: recorder.FakeMediaRecorder,
  })
  await capture.ready

  const done = capture.stop()

  await assert.rejects(done, { name: 'NotReadableError' })
  assert.equal(stream.stops, 1)
})
