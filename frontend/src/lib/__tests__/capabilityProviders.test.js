import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createCameraProvider,
  createMicrophoneProvider,
  builtInCapabilityProviders,
  createSpeechModelsProvider,
  createSpeechProvider,
} from '../capabilityProviders.js'
import { createDeviceStorageProvider, purgeDeviceStorage } from '../deviceStorage.js'
import { SCREEN_CONTROL } from '../screenControlHost.js'
import { createCapabilityHost } from '../capabilityHost.js'

function memoryLocalStorage() {
  const values = new Map()
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null },
    setItem(key, value) { values.set(key, String(value)) },
    removeItem(key) { values.delete(key) },
    key(index) { return [...values.keys()][index] ?? null },
    get length() { return values.size },
    values,
  }
}

async function invokeProvider(provider, input, limits = { max_bytes: 65536 }) {
  return new Promise((resolve, reject) => {
    Promise.resolve(provider.open({
      input,
      declaration: { version: 1, limits },
      channel: { result: resolve, error: reject, ready() {}, event() {} },
    })).catch(reject)
  })
}

test('device storage is isolated by installed app generation and supports JSON operations', async () => {
  const storage = memoryLocalStorage()
  const first = createDeviceStorageProvider({
    appId: 7,
    getIdentity: () => ({ appId: '7', appInstanceId: 'first-install' }),
    storage,
  })
  const replacement = createDeviceStorageProvider({
    appId: 7,
    getIdentity: () => ({ appId: '7', appInstanceId: 'replacement-install' }),
    storage,
  })

  assert.deepEqual(await invokeProvider(first, {
    operation: 'set', key: 'booking', value: { id: 'a' },
  }), { saved: true })
  assert.deepEqual(await invokeProvider(first, {
    operation: 'get', key: 'booking',
  }), { id: 'a' })
  assert.deepEqual(await invokeProvider(first, { operation: 'list' }), ['booking'])
  assert.equal(await invokeProvider(replacement, {
    operation: 'get', key: 'booking',
  }), null)
  assert.deepEqual(await invokeProvider(first, {
    operation: 'remove', key: 'booking',
  }), { removed: true })
  assert.equal(await invokeProvider(first, {
    operation: 'get', key: 'booking',
  }), null)
  assert.equal(storage.values.size, 1)
})

test('device storage enforces the reviewed byte ceiling', async () => {
  const provider = createDeviceStorageProvider({
    appId: 4,
    getIdentity: () => ({ appId: '4', appInstanceId: 'same-install' }),
    storage: memoryLocalStorage(),
  })
  await assert.rejects(
    invokeProvider(
      provider,
      { operation: 'set', key: 'large', value: 'x'.repeat(200) },
      { max_bytes: 64 },
    ),
    (error) => error.code === 'limit_exceeded',
  )
  await assert.rejects(
    invokeProvider(provider, {
      operation: 'set', key: 'not-json', value: { missing: undefined },
    }),
    TypeError,
  )
})

test('explicit app-data removal purges every installation partition for that app', () => {
  const storage = memoryLocalStorage()
  storage.setItem('mobius:device-storage:v1:7:first', '{}')
  storage.setItem('mobius:device-storage:v1:7:second', '{}')
  storage.setItem('mobius:device-storage:v1:8:first', '{}')
  storage.setItem('unrelated', 'keep')
  assert.equal(purgeDeviceStorage(7, storage), true)
  assert.deepEqual([...storage.values.keys()].sort(), [
    'mobius:device-storage:v1:8:first', 'unrelated',
  ])
})

test('microphone provider clamps app input to the reviewed manifest ceiling', async () => {
  let receivedSeconds
  let resolveDone
  const done = new Promise((resolve) => { resolveDone = resolve })
  const capture = {
    sampleRate: 48000,
    ready: Promise.resolve(),
    done,
    stop() { resolveDone({ samples: new Float32Array(0), sampleRate: 48000 }) },
    cancel() {},
  }
  const messages = []
  const provider = createMicrophoneProvider({
    startCapture: async ({ maxSeconds }) => {
      receivedSeconds = maxSeconds
      return capture
    },
  })
  const control = await provider.open({
    input: { maxDurationMs: 60_000 },
    declaration: { limits: { max_duration_ms: 8_000 } },
    channel: {
      ready(value) { messages.push(['ready', value]) },
      event() {},
      result(value) { messages.push(['result', value]) },
      error(error) { throw error },
    },
  })
  assert.equal(receivedSeconds, 8)
  assert.deepEqual(messages, [['ready', { sampleRate: 48000 }]])
  control.control('finish')
  await done
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(messages.at(-1)[0], 'result')
})

test('microphone capture can be cancelled while waiting for its first audio frame', async () => {
  let rejectReady
  let rejectDone
  let cancelCalls = 0
  const ready = new Promise((resolve, reject) => { rejectReady = reject })
  const done = new Promise((resolve, reject) => { rejectDone = reject })
  ready.catch(() => {})
  done.catch(() => {})
  const capture = {
    sampleRate: 48000,
    ready,
    done,
    stop() {},
    cancel() {
      cancelCalls += 1
      const error = new Error('Recording cancelled.')
      error.name = 'AbortError'
      rejectReady(error)
      rejectDone(error)
    },
  }
  const sent = []
  const source = {}
  const host = createCapabilityHost({
    providers: {
      'media.microphone.capture': createMicrophoneProvider({
        startCapture: async () => capture,
      }),
    },
    getDeclaration() {
      return { version: 1, limits: { max_duration_ms: 8000 } }
    },
    isActive: () => true,
    send(_source, message) { sent.push(message) },
  })

  host.handle(source, {
    type: 'moebius:capability-open',
    requestId: 'microphone-startup',
    capability: 'media.microphone.capture',
    version: 1,
    input: { maxDurationMs: 8000 },
  })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(sent.some((message) => message.type === 'moebius:capability-ready'), false)
  host.handle(source, {
    type: 'moebius:capability-control',
    requestId: 'microphone-startup',
    capability: 'media.microphone.capture',
    action: 'cancel',
  })
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(cancelCalls, 1)
  assert.ok(sent.some((message) => (
    message.type === 'moebius:capability-error' && message.name === 'AbortError'
  )))
})

test('camera provider clamps duration, reports progress, and transfers video bytes', async () => {
  let received
  let progress
  let resolveDone
  const done = new Promise((resolve) => { resolveDone = resolve })
  const bytes = new ArrayBuffer(4)
  const capture = {
    ready: Promise.resolve({
      mimeType: 'video/webm', width: 1920, height: 1080, audio: false,
    }),
    done,
    stop() {
      resolveDone({
        mimeType: 'video/webm', durationMs: 8000,
        width: 1920, height: 1080, bytes,
      })
    },
    cancel() {},
  }
  const sent = []
  const transfers = []
  const previews = []
  const provider = createCameraProvider({
    startCapture(options) {
      received = options
      progress = options.onProgress
      return capture
    },
    onPreview(value) { previews.push(value) },
  })
  const control = provider.open({
    input: {
      facingMode: 'environment', maxDurationMs: 60_000, audio: false,
    },
    declaration: {
      limits: { max_duration_ms: 8_000, max_bytes: 32 * 1024 * 1024 },
    },
    channel: {
      ready(value) { sent.push(['ready', value]) },
      event(event, value) { sent.push([event, value]) },
      result(value, transfer) {
        sent.push(['result', value])
        transfers.push(transfer)
      },
      error(error) { throw error },
    },
  })

  assert.equal(received.facingMode, 'environment')
  assert.equal(received.audio, false)
  assert.equal(received.maxDurationMs, 8_000)
  assert.equal(received.maxBytes, 32 * 1024 * 1024)
  const stream = { id: 'rear-camera' }
  control.control('preview-rect', { x: 12, y: 24, width: 320, height: 180 })
  received.onPreviewStream(stream)
  assert.deepEqual(previews.at(-1), {
    stream,
    rect: { x: 12, y: 24, width: 320, height: 180 },
    facingMode: 'environment',
  })
  await Promise.resolve()
  assert.equal(sent[0][0], 'ready')
  progress({ durationMs: 500, bytes: 4096 })
  control.control('finish')
  await done
  await new Promise((resolve) => setImmediate(resolve))

  assert.deepEqual(sent[1], ['progress', { durationMs: 500, bytes: 4096 }])
  assert.equal(sent.at(-1)[0], 'result')
  assert.equal(sent.at(-1)[1].bytes, bytes)
  assert.deepEqual(transfers, [[bytes]])
  received.onPreviewStream(null)
  assert.equal(previews.at(-1), null)
})

test('camera provider cancellation reaches a capture before camera readiness', async () => {
  let rejectReady
  let rejectDone
  let cancelCalls = 0
  const ready = new Promise((resolve, reject) => { rejectReady = reject })
  const done = new Promise((resolve, reject) => { rejectDone = reject })
  ready.catch(() => {})
  done.catch(() => {})
  const sent = []
  const source = {}
  const host = createCapabilityHost({
    providers: {
      'media.camera.capture': createCameraProvider({
        startCapture: () => ({
          ready,
          done,
          stop() {},
          cancel() {
            cancelCalls += 1
            const error = new Error('Video recording cancelled.')
            error.name = 'AbortError'
            rejectReady(error)
            rejectDone(error)
          },
        }),
      }),
    },
    getDeclaration: () => ({
      version: 1,
      limits: { max_duration_ms: 8000, max_bytes: 8 * 1024 * 1024 },
    }),
    isActive: () => true,
    send(_source, message) { sent.push(message) },
  })

  host.handle(source, {
    type: 'moebius:capability-open', requestId: 'camera-startup',
    capability: 'media.camera.capture', version: 1,
    input: { facingMode: 'environment', maxDurationMs: 8000, audio: false },
  })
  host.handle(source, {
    type: 'moebius:capability-control', requestId: 'camera-startup',
    capability: 'media.camera.capture', action: 'cancel',
  })
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(cancelCalls, 1)
  assert.equal(sent.some((message) => (
    message.type === 'moebius:capability-ready'
  )), false)
  assert.equal(sent.some((message) => (
    message.type === 'moebius:capability-error' && message.name === 'AbortError'
  )), true)
})

test('camera provider rejects unreviewed input fields before opening a device', () => {
  let starts = 0
  const provider = createCameraProvider({
    startCapture() {
      starts += 1
      throw new Error('should not start')
    },
  })
  const channel = { event() {}, ready() {}, result() {}, error() {} }
  const declaration = {
    limits: { max_duration_ms: 8000, max_bytes: 8 * 1024 * 1024 },
  }

  assert.throws(() => provider.open({
    input: { facingMode: 'left', maxDurationMs: 1000, audio: false },
    declaration,
    channel,
  }), /facingMode/)
  assert.throws(() => provider.open({
    input: { facingMode: 'environment', maxDurationMs: '1000', audio: false },
    declaration,
    channel,
  }), /positive number/)
  assert.throws(() => provider.open({
    input: { facingMode: 'environment', maxDurationMs: 1000, preview: true },
    declaration,
    channel,
  }), /Unknown camera capture input/)
  assert.equal(starts, 0)
})

test('speech providers lazy-load the runtime and preserve the invoking app identity', async () => {
  const calls = []
  const runtime = {
    openSpeechCapability(context) {
      calls.push(['speech', context.input])
      return { control() {} }
    },
    openSpeechModelsCapability(context) {
      calls.push(['models', context.appId, context.input])
      return { control() {} }
    },
  }
  const loadRuntime = async () => runtime
  const channel = {}
  const speech = createSpeechProvider({ loadRuntime })
  const models = createSpeechModelsProvider({ appId: 61, loadRuntime })

  const speechControl = await speech.open({ input: { text: 'Hello' }, channel })
  const modelControl = await models.open({ input: { operation: 'catalog' }, channel })

  assert.equal(typeof speechControl.control, 'function')
  assert.equal(typeof modelControl.control, 'function')
  assert.equal(speech.contention({
    input: { operation: 'catalog' }, activeInput: { operation: 'model-stream' },
  }), 'share')
  assert.equal(speech.contention({
    input: { operation: 'model-stream' }, activeInput: { operation: 'synthesize' },
  }), 'replace')
  assert.deepEqual(calls, [
    ['speech', { text: 'Hello' }],
    ['models', 61, { operation: 'catalog' }],
  ])
})

test('an invalid speech operation cannot replace healthy work', async () => {
  const controls = []
  const sent = []
  const source = { id: 'speech-frame' }
  const provider = createSpeechProvider({
    loadRuntime: async () => ({
      openSpeechCapability({ input }) {
        if (input.operation === 'unknown') {
          throw new TypeError('Unknown speech operation.')
        }
        return { control(action) { controls.push(action) } }
      },
    }),
  })
  const host = createCapabilityHost({
    providers: { 'media.speech': provider },
    getDeclaration: () => ({ version: 1, lifecycle: 'background' }),
    isActive: () => true,
    send(_target, message) { sent.push(message) },
  })

  host.handle(source, {
    type: 'moebius:capability-open',
    requestId: 'healthy-stream',
    capability: 'media.speech',
    version: 1,
    input: { operation: 'model-stream' },
  })
  await new Promise((resolve) => setImmediate(resolve))

  host.handle(source, {
    type: 'moebius:capability-open',
    requestId: 'invalid-stream',
    capability: 'media.speech',
    version: 1,
    input: { operation: 'unknown' },
  })
  await new Promise((resolve) => setImmediate(resolve))

  assert.deepEqual(controls, [])
  assert.equal(host.activeCount(), 1)
  assert.equal(sent.some((message) => (
    message.requestId === 'invalid-stream'
      && message.type === 'moebius:capability-error'
      && message.code === 'invalid_request'
  )), true)
})

test('screen control provider binds the app chat, survives detach, and reattaches', async () => {
  const sent = []
  const capture = {
    stream: { getTracks: () => [{ stop() {} }] },
    video: { srcObject: {} },
  }
  let clientOptions
  let stopCalls = 0
  const provider = builtInCapabilityProviders({
    screenControl: {
      // App route params reach the canvas as strings.
      appId: '91',
      requestCapture: async () => capture,
      startSession: async (payload) => {
        sent.push(payload)
        return { sessionId: 'session-1', expiresAt: 12345 }
      },
      makeClient(options) {
        clientOptions = options
        return { async stop() { stopCalls += 1 } }
      },
    },
  })[SCREEN_CONTROL]
  const messages = []
  const control = await provider.open({
    input: { chatId: 'chat-1' },
    channel: {
      ready(value) { messages.push(['ready', value]) },
      result(value) { messages.push(['result', value]) },
      error(error) { throw error },
    },
  })

  assert.equal(sent[0].appId, 91)
  assert.equal(sent[0].chatId, 'chat-1')
  clientOptions.onConnected()
  assert.deepEqual(messages, [['ready', { expiresAt: 12345 }]])

  control.control('detach')
  assert.equal(stopCalls, 0)

  const resumedMessages = []
  const resumed = await provider.open({
    input: { chatId: 'chat-1', resume: true },
    channel: {
      ready(value) { resumedMessages.push(['ready', value]) },
      result(value) { resumedMessages.push(['result', value]) },
      error(error) { throw error },
    },
  })
  assert.deepEqual(resumedMessages, [['ready', { expiresAt: 12345 }]])

  resumed.control('finish')
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(stopCalls, 1)
  assert.deepEqual(resumedMessages.at(-1), ['result', { reason: 'owner' }])
})

test('an invalid speech operation cannot replace healthy work', async () => {
  const controls = []
  const sent = []
  const source = { id: 'speech-frame' }
  const provider = createSpeechProvider({
    loadRuntime: async () => ({
      openSpeechCapability({ input }) {
        if (input.operation === 'unknown') {
          throw new TypeError('Unknown speech operation.')
        }
        return { control(action) { controls.push(action) } }
      },
    }),
  })
  const host = createCapabilityHost({
    providers: { 'media.speech': provider },
    getDeclaration: () => ({ version: 1, lifecycle: 'background' }),
    isActive: () => true,
    send(_target, message) { sent.push(message) },
  })

  host.handle(source, {
    type: 'moebius:capability-open',
    requestId: 'healthy-stream',
    capability: 'media.speech',
    version: 1,
    input: { operation: 'model-stream' },
  })
  await new Promise((resolve) => setImmediate(resolve))

  host.handle(source, {
    type: 'moebius:capability-open',
    requestId: 'invalid-stream',
    capability: 'media.speech',
    version: 1,
    input: { operation: 'unknown' },
  })
  await new Promise((resolve) => setImmediate(resolve))

  assert.deepEqual(controls, [])
  assert.equal(host.activeCount(), 1)
  assert.equal(sent.some((message) => (
    message.requestId === 'invalid-stream'
      && message.type === 'moebius:capability-error'
      && message.code === 'invalid_request'
  )), true)
})
