import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createMicrophoneProvider,
  createSpeechModelsProvider,
  createSpeechProvider,
} from '../capabilityProviders.js'

test('microphone provider clamps app input to the reviewed manifest ceiling', async () => {
  let receivedSeconds
  let resolveDone
  const done = new Promise((resolve) => { resolveDone = resolve })
  const capture = {
    sampleRate: 48000,
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
  await Promise.resolve()
  assert.equal(messages.at(-1)[0], 'result')
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
