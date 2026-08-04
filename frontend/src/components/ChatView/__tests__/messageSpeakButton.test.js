import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  chatSpeechSnapshot,
  chatSpeechKey,
  messageSpeechText,
  stopChatSpeech,
  toggleChatSpeech,
} from '../chatSpeechPlayer.js'

test('message speech removes markdown chrome and omits fenced code', () => {
  assert.equal(
    messageSpeechText('# Result\n\nRead **this** [source](https://example.com).\n```js\nalert(1)\n```'),
    'Result Read this source. Code example omitted.',
  )
})

test('missing browser audio support becomes a retryable message error', async () => {
  await toggleChatSpeech({
    chatId: 'chat-a', messageKey: 'assistant-1', text: 'Read this',
  }, { AudioContext: null })
  assert.deepEqual(chatSpeechSnapshot(), {
    key: chatSpeechKey('chat-a', 'assistant-1'),
    phase: 'error',
    error: 'This browser cannot play generated speech.',
  })
  stopChatSpeech()
})

test('message speech resumes audio before loading the shared runtime', async () => {
  const order = []
  class AudioContext {
    state = 'running'
    currentTime = 0
    async resume() { order.push('resume') }
    async close() { this.state = 'closed' }
  }
  await toggleChatSpeech({
    chatId: 'chat-a', messageKey: 'assistant-2', text: 'Read this',
  }, {
    AudioContext,
    async loadSpeech() {
      order.push('load')
      return {
        async speechModelCatalog() { return { activeModelId: 'voice', models: [] } },
        synthesizeSpeech() { throw new Error('must not synthesize without a model') },
      }
    },
  })
  assert.deepEqual(order, ['resume', 'load'])
  assert.match(chatSpeechSnapshot().error, /download a speech model/)
  stopChatSpeech()
})

test('toggling the active message stops its pending synthesis', async () => {
  let cancelled = false
  let resolveResult
  class AudioContext {
    state = 'running'
    currentTime = 0
    async resume() {}
    async close() { this.state = 'closed' }
  }
  const first = toggleChatSpeech({
    chatId: 'chat-a', messageKey: 'assistant-3', text: 'Read this',
  }, {
    AudioContext,
    async loadSpeech() {
      return {
        async speechModelCatalog() {
          return { activeModelId: 'voice', models: [{ id: 'voice', state: 'ready' }] }
        },
        synthesizeSpeech() {
          return {
            cancel() { cancelled = true; resolveResult() },
            result: new Promise(resolve => { resolveResult = resolve }),
          }
        },
      }
    },
  })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(chatSpeechSnapshot().phase, 'loading')
  await toggleChatSpeech({
    chatId: 'chat-a', messageKey: 'assistant-3', text: 'Read this',
  })
  await first
  assert.equal(cancelled, true)
  assert.deepEqual(chatSpeechSnapshot(), { key: '', phase: 'idle', error: '' })
})

test('an unrelated chat cleanup cannot stop a colliding message key', async () => {
  let cancelled = false
  let resolveResult
  class AudioContext {
    state = 'running'
    currentTime = 0
    async resume() {}
    async close() { this.state = 'closed' }
  }
  const playing = toggleChatSpeech({
    chatId: 'chat-a', messageKey: 'assistant-1', text: 'Read this',
  }, {
    AudioContext,
    async loadSpeech() {
      return {
        async speechModelCatalog() {
          return { activeModelId: 'voice', models: [{ id: 'voice', state: 'ready' }] }
        },
        synthesizeSpeech() {
          return {
            cancel() { cancelled = true; resolveResult() },
            result: new Promise(resolve => { resolveResult = resolve }),
          }
        },
      }
    },
  })
  await new Promise(resolve => setTimeout(resolve, 0))

  assert.notEqual(
    chatSpeechKey('chat-a', 'assistant-1'),
    chatSpeechKey('chat-b', 'assistant-1'),
  )
  assert.equal(stopChatSpeech({ chatId: 'chat-b' }), false)
  assert.equal(cancelled, false)
  assert.equal(
    chatSpeechSnapshot().key,
    chatSpeechKey('chat-a', 'assistant-1'),
  )

  assert.equal(stopChatSpeech({ chatId: 'chat-a' }), true)
  await playing
  assert.equal(cancelled, true)
  assert.deepEqual(chatSpeechSnapshot(), { key: '', phase: 'idle', error: '' })
})

test('stopping during catalog lookup never starts stale synthesis', async () => {
  let resolveCatalog
  let synthesized = false
  let closed = false
  class AudioContext {
    state = 'running'
    currentTime = 0
    async resume() {}
    async close() { this.state = 'closed'; closed = true }
  }
  const playing = toggleChatSpeech({
    chatId: 'chat-catalog', messageKey: 'assistant-1', text: 'Read this',
  }, {
    AudioContext,
    async loadSpeech() {
      return {
        speechModelCatalog() {
          return new Promise(resolve => { resolveCatalog = resolve })
        },
        synthesizeSpeech() { synthesized = true; throw new Error('stale synthesis') },
      }
    },
  })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(stopChatSpeech({ chatId: 'chat-catalog' }), true)
  resolveCatalog({
    activeModelId: 'voice',
    models: [{ id: 'voice', state: 'ready' }],
  })
  await playing
  assert.equal(synthesized, false)
  assert.equal(closed, true)
})

test('stopping queued playback cancels its wait and closes audio promptly', async () => {
  let sourceStopped = false
  let closed = false
  class AudioContext {
    state = 'running'
    currentTime = 0
    destination = {}
    async resume() {}
    async close() { this.state = 'closed'; closed = true }
    createBuffer() {
      return { duration: 60, copyToChannel() {} }
    }
    createBufferSource() {
      return {
        connect() {},
        start() {},
        stop() { sourceStopped = true },
      }
    }
  }
  const playing = toggleChatSpeech({
    chatId: 'chat-wait', messageKey: 'assistant-1', text: 'Read this',
  }, {
    AudioContext,
    async loadSpeech() {
      return {
        async speechModelCatalog() {
          return {
            activeModelId: 'voice',
            models: [{ id: 'voice', state: 'ready', sampleRate: 24_000 }],
          }
        },
        synthesizeSpeech({ onAudio }) {
          onAudio(new Float32Array([0.25]))
          return { result: Promise.resolve(), cancel() {} }
        },
      }
    },
  })
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(chatSpeechSnapshot().phase, 'playing')
  assert.equal(stopChatSpeech({ chatId: 'chat-wait' }), true)
  await Promise.race([
    playing,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error('playback cleanup did not settle promptly')),
      100,
    )),
  ])
  assert.equal(sourceStopped, true)
  assert.equal(closed, true)
})
