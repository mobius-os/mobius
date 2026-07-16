import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  normalizeMicrophoneSeconds,
  startMicrophoneCapture,
} from '../microphoneCapture.js'

test('microphone duration is finite and bounded', () => {
  assert.equal(normalizeMicrophoneSeconds(undefined), 30)
  assert.equal(normalizeMicrophoneSeconds(0), 0.1)
  assert.equal(normalizeMicrophoneSeconds(8), 8)
  assert.equal(normalizeMicrophoneSeconds(999), 60)
})

test('shell microphone capture returns mono PCM and releases every resource', async () => {
  let processor
  let trackStopped = false
  let contextClosed = false
  const levels = []
  const node = () => ({ connect() {}, disconnect() {} })

  class FakeAudioContext {
    constructor() {
      this.sampleRate = 100
      this.state = 'running'
      this.destination = node()
    }
    createMediaStreamSource() { return node() }
    createScriptProcessor() {
      processor = { ...node(), onaudioprocess: null }
      return processor
    }
    createGain() { return { ...node(), gain: { value: 1 } } }
    close() { contextClosed = true }
  }

  const control = await startMicrophoneCapture({
    mediaDevices: {
      async getUserMedia() {
        return { getTracks: () => [{ stop: () => { trackStopped = true } }] }
      },
    },
    AudioContextCtor: FakeAudioContext,
    maxSeconds: 1,
    onLevel: (level) => levels.push(level),
  })

  processor.onaudioprocess({
    inputBuffer: { getChannelData: () => new Float32Array([0.25, -0.75, 0.5]) },
  })
  const resultPromise = control.stop()
  const result = await resultPromise

  assert.equal(result.sampleRate, 100)
  assert.deepEqual([...result.samples], [0.25, -0.75, 0.5])
  assert.deepEqual(levels, [0.75])
  assert.equal(trackStopped, true)
  assert.equal(contextClosed, true)
  assert.equal(processor.onaudioprocess, null)
})

test('cancelling shell capture rejects and releases every resource', async () => {
  let processor
  let trackStops = 0
  let contextCloses = 0
  const node = () => ({ connect() {}, disconnect() {} })

  class FakeAudioContext {
    constructor() {
      this.sampleRate = 48000
      this.state = 'running'
      this.destination = node()
    }
    createMediaStreamSource() { return node() }
    createScriptProcessor() {
      processor = { ...node(), onaudioprocess: null }
      return processor
    }
    createGain() { return { ...node(), gain: { value: 1 } } }
    close() { contextCloses += 1 }
  }

  const control = await startMicrophoneCapture({
    mediaDevices: {
      async getUserMedia() {
        return { getTracks: () => [{ stop: () => { trackStops += 1 } }] }
      },
    },
    AudioContextCtor: FakeAudioContext,
  })

  await assert.rejects(control.cancel(), { name: 'AbortError' })
  assert.equal(trackStops, 1)
  assert.equal(contextCloses, 1)
  assert.equal(processor.onaudioprocess, null)

  // Cancellation is idempotent and cannot stop/close the same resources twice.
  await assert.rejects(control.cancel(), { name: 'AbortError' })
  assert.equal(trackStops, 1)
  assert.equal(contextCloses, 1)
})
