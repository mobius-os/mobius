import { test } from 'node:test'
import assert from 'node:assert/strict'
import { webcrypto } from 'node:crypto'

import { PocketTtsWorkerRuntime } from '../speech/pocketTtsEngine.js'
import {
  DEFAULT_SPEECH_MODEL_ID,
  publicSpeechModel,
  speechModel,
} from '../speech/speechModels.js'
import { selectSpeechModel, speechModelCatalog } from '../speech/speechModelStore.js'
import { SpeechGenerationOwnership } from '../speech/speechGenerationOwnership.js'

function runtimeWithWorker() {
  const messages = []
  const runtime = new PocketTtsWorkerRuntime()
  runtime.worker = {
    postMessage(value, transfer = []) { messages.push({ value, transfer }) },
  }
  return { runtime, messages }
}

test('speech model selection exposes only the public catalog contract', () => {
  const selected = speechModel(DEFAULT_SPEECH_MODEL_ID)
  assert.equal(selected.id, 'pocket-tts-alba')
  assert.equal(speechModel('missing'), null)
  assert.deepEqual(Object.keys(publicSpeechModel(selected)).sort(), [
    'description', 'engine', 'id', 'language', 'name', 'sampleRate', 'storedBytes', 'voice',
  ])
})

test('model store reports a missing shared model and refuses to select it', async () => {
  const options = {
    dependencies: {
      cacheStorage: {
        async keys() { return [] },
        async open() { throw new Error('missing partitions must not be opened') },
      },
      cryptoImpl: webcrypto,
      origin: 'https://mobius.test',
      storageManager: {},
    },
  }
  const catalog = await speechModelCatalog(options)
  assert.equal(catalog.activeModelId, DEFAULT_SPEECH_MODEL_ID)
  assert.equal(catalog.models[0].state, 'missing')
  await assert.rejects(
    selectSpeechModel(DEFAULT_SPEECH_MODEL_ID, options),
    (error) => error.code === 'not_installed',
  )
})

test('worker chunk transfer stays pending until the worker acknowledges ownership', async () => {
  const { runtime, messages } = runtimeWithWorker()
  let settled = false
  const bytes = new ArrayBuffer(8)
  const accepted = runtime.sendChunk({
    assetId: 'model', index: 0, offset: 0, bytes,
  }).then(() => { settled = true })
  await Promise.resolve()
  assert.equal(settled, false)
  assert.equal(messages[0].transfer[0], bytes)
  runtime.onMessage({ type: 'chunk-accepted', chunkId: messages[0].value.chunkId })
  await accepted
  assert.equal(settled, true)
})

test('worker generation forwards audio and settles only its matching request', async () => {
  const { runtime, messages } = runtimeWithWorker()
  const audio = []
  const result = runtime.generate('Hello', { onChunk: (samples) => audio.push(samples) })
  const requestId = messages[0].value.requestId
  const samples = new Float32Array([0.25])
  runtime.onMessage({ type: 'audio', requestId, samples })
  runtime.onMessage({ type: 'generate-complete', requestId: 'another-request' })
  await Promise.resolve()
  assert.deepEqual(audio, [samples])
  runtime.onMessage({ type: 'generate-complete', requestId })
  await result
})

test('aborting generation cancels the worker and rejects with AbortError', async () => {
  const { runtime, messages } = runtimeWithWorker()
  const controller = new AbortController()
  const result = runtime.generate('Hello', { signal: controller.signal })
  controller.abort()
  await assert.rejects(result, (error) => error.name === 'AbortError')
  assert.equal(messages.at(-1).value.type, 'cancel-generate')
  assert.equal(runtime.generations.size, 0)
})

test('worker errors reject pending generation and release its lifecycle state', async () => {
  const { runtime, messages } = runtimeWithWorker()
  const result = runtime.generate('Hello')
  const requestId = messages[0].value.requestId
  runtime.onMessage({
    type: 'generate-error', requestId,
    error: { name: 'RangeError', message: 'model failed' },
  })
  await assert.rejects(result, (error) => (
    error.name === 'RangeError' && error.message === 'model failed'
  ))
  assert.equal(runtime.generations.size, 0)
})

test('an unwinding generation cannot release the successor that replaced it', () => {
  const ownership = new SpeechGenerationOwnership()
  ownership.claim('request-a')
  ownership.cancel()
  ownership.claim('request-b')

  assert.equal(ownership.release('request-a'), false)
  assert.equal(ownership.owns('request-b'), true)
  assert.equal(ownership.release('request-b'), true)
  assert.equal(ownership.owns('request-b'), false)
})
