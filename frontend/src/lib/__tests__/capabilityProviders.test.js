import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createMicrophoneProvider,
  builtInCapabilityProviders,
  createSpeechModelsProvider,
  createSpeechProvider,
} from '../capabilityProviders.js'
import { SCREEN_CONTROL } from '../screenControlHost.js'
import { createCapabilityHost } from '../capabilityHost.js'

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
  assert.deepEqual(calls, [
    ['speech', { text: 'Hello' }],
    ['models', 61, { operation: 'catalog' }],
  ])
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
