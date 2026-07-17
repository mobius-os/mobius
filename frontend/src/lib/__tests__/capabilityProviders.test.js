import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createMicrophoneProvider } from '../capabilityProviders.js'

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
