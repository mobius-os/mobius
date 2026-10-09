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
  const navigator = { audioSession: { type: 'playback' } }
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
    navigator,
    mediaDevices: {
      async getUserMedia() {
        assert.equal(navigator.audioSession.type, 'play-and-record')
        return { getTracks: () => [{ stop: () => { trackStopped = true } }] }
      },
    },
    AudioContextCtor: FakeAudioContext,
    maxSeconds: 1,
    onLevel: (level) => levels.push(level),
  })

  const ready = control.ready
  assert.equal(navigator.audioSession.type, 'play-and-record')
  processor.onaudioprocess({
    inputBuffer: { getChannelData: () => new Float32Array([0.25, -0.75, 0.5]) },
  })
  await ready
  const resultPromise = control.stop()
  const result = await resultPromise

  assert.equal(result.sampleRate, 100)
  assert.deepEqual([...result.samples], [0.25, -0.75, 0.5])
  assert.deepEqual(levels, [0.75])
  assert.equal(trackStopped, true)
  assert.equal(contextClosed, true)
  assert.equal(processor.onaudioprocess, null)
  assert.equal(navigator.audioSession.type, 'playback')
})

test('microphone permission denial restores playback routing', async () => {
  const navigator = { audioSession: { type: 'playback' } }
  await assert.rejects(startMicrophoneCapture({
    navigator,
    AudioContextCtor: class { close() {} },
    mediaDevices: { async getUserMedia() { throw new Error('denied') } },
  }), /denied/)
  assert.equal(navigator.audioSession.type, 'playback')
})

test('audio context activation is requested before the microphone permission promise', async () => {
  const order = []
  let resolveStream
  let processor
  const stream = { getTracks: () => [{ stop() {} }] }
  const node = () => ({ connect() {}, disconnect() {} })
  class FakeAudioContext {
    constructor() {
      order.push('context')
      this.sampleRate = 48_000
      this.state = 'suspended'
      this.destination = node()
    }
    resume() {
      order.push('resume')
      this.state = 'running'
      return Promise.resolve()
    }
    createMediaStreamSource() { return node() }
    createScriptProcessor() {
      processor = { ...node(), onaudioprocess: null }
      return processor
    }
    createGain() { return { ...node(), gain: { value: 1 } } }
    close() {}
  }

  const start = startMicrophoneCapture({
    mediaDevices: {
      getUserMedia() {
        order.push('getUserMedia')
        return new Promise((resolve) => { resolveStream = resolve })
      },
    },
    AudioContextCtor: FakeAudioContext,
  })
  await Promise.resolve()
  assert.deepEqual(order, ['context', 'resume', 'getUserMedia'])

  resolveStream(stream)
  const control = await start
  await assert.rejects(control.cancel(), { name: 'AbortError' })
  assert.equal(processor.onaudioprocess, null)
})

test('finishing before any microphone frame rejects instead of returning silence', async () => {
  let processor
  const node = () => ({ connect() {}, disconnect() {} })
  class FakeAudioContext {
    constructor() {
      this.sampleRate = 48_000
      this.state = 'running'
      this.destination = node()
    }
    createMediaStreamSource() { return node() }
    createScriptProcessor() {
      processor = { ...node(), onaudioprocess: null }
      return processor
    }
    createGain() { return { ...node(), gain: { value: 1 } } }
    close() {}
  }

  const control = await startMicrophoneCapture({
    mediaDevices: {
      async getUserMedia() { return { getTracks: () => [{ stop() {} }] } },
    },
    AudioContextCtor: FakeAudioContext,
  })
  const ready = control.ready
  await assert.rejects(control.stop(), { name: 'NotReadableError' })
  await assert.rejects(ready, { name: 'NotReadableError' })
  assert.equal(processor.onaudioprocess, null)
})

test('capture reports startup failure when no microphone frame arrives', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let processor
  let trackStopped = false
  let contextClosed = false
  const node = () => ({ connect() {}, disconnect() {} })
  class FakeAudioContext {
    constructor() {
      this.sampleRate = 48_000
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
      async getUserMedia() { return { getTracks: () => [{ stop: () => { trackStopped = true } }] } },
    },
    AudioContextCtor: FakeAudioContext,
  })
  const ready = control.ready
  const done = control.done
  t.mock.timers.tick(5_000)

  await assert.rejects(ready, { name: 'NotReadableError' })
  await assert.rejects(done, { name: 'NotReadableError' })
  assert.equal(trackStopped, true)
  assert.equal(contextClosed, true)
  assert.equal(processor.onaudioprocess, null)
})

test('an immediate first frame cannot leave the startup timeout armed', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let processor
  let sourceConnected = false
  const node = () => ({ connect() {}, disconnect() {} })
  const frame = {
    inputBuffer: { getChannelData: () => new Float32Array([0.25, -0.5, 0.125]) },
  }
  class FakeAudioContext {
    constructor() {
      this.sampleRate = 48_000
      this.state = 'running'
      this.destination = node()
    }
    createMediaStreamSource() {
      return {
        ...node(),
        connect() {
          sourceConnected = true
          processor.onaudioprocess?.(frame)
        },
      }
    }
    createScriptProcessor() {
      let handler = null
      processor = { ...node() }
      Object.defineProperty(processor, 'onaudioprocess', {
        get: () => handler,
        set(value) {
          handler = value
          if (sourceConnected) handler?.(frame)
        },
      })
      return processor
    }
    createGain() { return { ...node(), gain: { value: 1 } } }
    close() {}
  }

  const control = await startMicrophoneCapture({
    mediaDevices: {
      async getUserMedia() { return { getTracks: () => [{ stop() {} }] } },
    },
    AudioContextCtor: FakeAudioContext,
    maxSeconds: 60,
  })
  await control.ready
  let done = false
  control.done.then(() => { done = true })
  t.mock.timers.tick(5_000)
  await Promise.resolve()

  assert.equal(done, false)
  await control.stop()
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

  const ready = control.ready
  await assert.rejects(control.cancel(), { name: 'AbortError' })
  await assert.rejects(ready, { name: 'AbortError' })
  assert.equal(trackStops, 1)
  assert.equal(contextCloses, 1)
  assert.equal(processor.onaudioprocess, null)

  // Cancellation is idempotent and cannot stop/close the same resources twice.
  await assert.rejects(control.cancel(), { name: 'AbortError' })
  assert.equal(trackStops, 1)
  assert.equal(contextCloses, 1)
})
