import { test, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  releaseSpeechEngine, retainSpeechEngine, synthesizeInFrame,
} from '../../runtime/speech.js'
import { makeCapabilities } from '../../runtime/capabilities.js'

const tick = () => new Promise((resolve) => setImmediate(resolve))
const MANIFEST = Object.freeze({
  assetBytes: { tokenizer: 4, model: 4, voice: 4 },
  temperature: 0.3,
  clonedVoiceSamples: null,
})

let workers
let fetches
let saved

// The speech worker's protocol, answered at once. A `generate` whose text is
// `hold` keeps generating until the test finishes it, like a long block.
class FakeWorker {
  constructor() {
    this.messages = []
    this.terminated = false
    this.held = null
    workers.push(this)
  }

  reply(data) {
    setImmediate(() => { if (!this.terminated) this.onmessage?.({ data }) })
  }

  postMessage(message) {
    this.messages.push(message)
    if (message.type === 'load-start') this.reply({ type: 'load-ready' })
    if (message.type === 'asset-chunk') this.reply({ type: 'chunk-accepted', chunkId: message.chunkId })
    if (message.type === 'load-finish') this.reply({ type: 'load-complete' })
    if (message.type === 'generate') {
      const { requestId } = message
      this.reply({ type: 'audio', requestId, samples: new Float32Array([0.1]) })
      if (message.text === 'hold') this.held = requestId
      else this.reply({ type: 'generate-complete', requestId })
    }
  }

  terminate() { this.terminated = true }
}

function openModelStream(manifest, streams) {
  return ({ onManifest, onChunk, onComplete, onError }) => {
    const stream = {
      chunks: 0,
      cancelled: false,
      control(action) {
        if (stream.cancelled) return
        if (action === 'start') {
          stream.chunks += 1
          setImmediate(() => onChunk({ index: 0, assetId: 'model', offset: 0, bytes: new ArrayBuffer(4) }))
        }
        if (action === 'chunk-accepted') setImmediate(onComplete)
      },
      cancel() {
        stream.cancelled = true
        const error = new Error('Capability request cancelled.')
        error.name = 'AbortError'
        setImmediate(() => onError(error))
      },
    }
    streams.push(stream)
    setImmediate(() => onManifest(manifest))
    return stream
  }
}

function speak(input, { manifest = MANIFEST, streams = [] } = {}) {
  const events = { audio: [], boundary: [] }
  const session = synthesizeInFrame({
    input,
    maxTextChars: 50_000,
    channel: { event(name, value) { events[name]?.push(value) } },
    openModelStream: openModelStream(manifest, streams),
  })
  return { session, events, streams }
}

async function until(condition) {
  for (let attempt = 0; attempt < 50 && !condition(); attempt += 1) await tick()
  assert.ok(condition(), 'the fake engine reached the expected state')
}

beforeEach(() => {
  workers = []
  fetches = 0
  saved = { Worker: globalThis.Worker, fetch: globalThis.fetch, location: globalThis.location }
  globalThis.Worker = FakeWorker
  globalThis.fetch = async () => {
    fetches += 1
    return { ok: true, async text() { return '// speech worker' } }
  }
  globalThis.location = { href: 'https://app.mobius.test/' }
  retainSpeechEngine(true)
})

afterEach(() => {
  releaseSpeechEngine()
  retainSpeechEngine(true)
  globalThis.Worker = saved.Worker
  globalThis.fetch = saved.fetch
  globalThis.location = saved.location
})

test('a second session for the same model reuses the loaded engine', async () => {
  const first = speak({ text: 'One.', modelId: 'alba' })
  await first.session.result
  const second = speak({ text: 'Two.', modelId: 'alba' })
  await second.session.result

  assert.equal(workers.length, 1)
  assert.equal(fetches, 1)
  assert.equal(workers[0].terminated, false)
  assert.equal(workers[0].messages.filter(({ type }) => type === 'load-start').length, 1)
  assert.equal(second.streams[0].cancelled, true, 'the reused session releases its model stream')
  assert.equal(second.streams[0].chunks, 0, 'no model bytes are streamed again')
  assert.equal(second.events.audio.length, 1)
  assert.deepEqual(second.events.boundary, [{ segmentIndex: 0, kind: 'paragraph', pauseAfterMs: 0 }])
})

test('a different model or a re-recorded voice replaces the idle engine', async () => {
  await speak({ text: 'One.', modelId: 'alba' }).session.result
  await speak({ text: 'Two.', modelId: 'other' }).session.result
  assert.equal(workers.length, 2)
  assert.equal(workers[0].terminated, true, 'the previous model is freed')

  const voice = (value) => ({ ...MANIFEST, clonedVoiceSamples: new Float32Array([value, value]) })
  await speak({ text: 'Mine.', modelId: 'clone' }, { manifest: voice(0.5) }).session.result
  await speak({ text: 'Mine.', modelId: 'clone' }, { manifest: voice(0.5) }).session.result
  assert.equal(workers.length, 3, 'the same recording reuses its engine')
  await speak({ text: 'Mine.', modelId: 'clone' }, { manifest: voice(0.25) }).session.result
  assert.equal(workers.length, 4)
  assert.equal(workers[2].terminated, true)
})

test('a hidden app keeps no idle engine', async () => {
  await speak({ text: 'One.', modelId: 'alba' }).session.result
  retainSpeechEngine(false)
  assert.equal(workers[0].terminated, true)

  // A session that ends while hidden gives its engine back too.
  await speak({ text: 'Two.', modelId: 'alba' }).session.result
  assert.equal(workers.length, 2)
  assert.equal(workers[1].terminated, true)

  retainSpeechEngine(true)
  await speak({ text: 'Three.', modelId: 'alba' }).session.result
  assert.equal(workers.length, 3)
  assert.equal(workers[2].terminated, false)
})

test('cancelling keeps the loaded engine and its late audio stays out of the next session', async () => {
  const first = speak({ text: 'hold', modelId: 'alba' })
  await until(() => workers[0]?.held)
  const cancelledRequest = workers[0].held
  first.session.cancel()
  await assert.rejects(first.session.result, { name: 'AbortError' })
  assert.equal(workers[0].messages.at(-1).type, 'cancel-generate')
  assert.equal(workers[0].terminated, false)

  const second = speak({ text: 'Next.', modelId: 'alba' })
  await until(() => second.streams[0]?.cancelled)
  workers[0].reply({ type: 'audio', requestId: cancelledRequest, samples: new Float32Array([0.9]) })
  workers[0].reply({ type: 'generate-complete', requestId: cancelledRequest })
  await second.session.result

  assert.equal(workers.length, 1)
  assert.deepEqual(second.events.audio.map(({ samples }) => samples[0]), [Float32Array.of(0.1)[0]])
  assert.equal(second.events.boundary.length, 1)
})

test('an engine that failed or never loaded is discarded', async () => {
  const early = speak({ text: 'One.', modelId: 'alba' })
  early.session.cancel()
  await assert.rejects(early.session.result, { name: 'AbortError' })
  await tick()
  assert.equal(workers.every((worker) => worker.terminated), true)

  const failing = speak({ text: 'hold', modelId: 'alba' })
  const index = workers.length
  await until(() => workers[index]?.held)
  workers[index].reply({ type: 'generate-error', requestId: workers[index].held, error: { message: 'boom' } })
  await assert.rejects(failing.session.result, { code: 'engine_failed' })
  assert.equal(workers[index].terminated, true)

  await speak({ text: 'Two.', modelId: 'alba' }).session.result
  assert.equal(workers.length, index + 2)
})

test('a long block reports one boundary with the caller segment index', async () => {
  const long = Array.from({ length: 12 }, (_, i) => `Sentence number ${i} carries some words.`).join(' ')
  assert.ok(long.length > 200)
  const { session, events } = speak({
    modelId: 'alba',
    document: {
      version: 1,
      segments: [
        { text: 'Title.', kind: 'title', pauseAfterMs: 400 },
        { text: long, kind: 'paragraph', pauseAfterMs: 700 },
        { text: 'End.', kind: 'paragraph', pauseAfterMs: 0 },
      ],
    },
  })
  const { segmentCount } = await session.result

  assert.ok(segmentCount > 3, 'the long block was generated in pieces')
  assert.equal(events.audio.length, segmentCount)
  assert.deepEqual(events.boundary, [
    { segmentIndex: 0, kind: 'title', pauseAfterMs: 400 },
    { segmentIndex: 1, kind: 'paragraph', pauseAfterMs: 700 },
    { segmentIndex: 2, kind: 'paragraph', pauseAfterMs: 0 },
  ])
})

// The frame host: answers each `model-stream` request the way the shell does,
// and lets the test drive the app's visibility.
function fakeHost() {
  const listeners = new Set()
  const visibility = {
    visible: true,
    callbacks: new Set(),
    onVisibilityChange(callback) {
      this.callbacks.add(callback)
      callback(this.visible)
      return () => this.callbacks.delete(callback)
    },
    set(next) {
      this.visible = next
      for (const callback of this.callbacks) callback(next)
    },
  }
  const parent = {
    postMessage(data) {
      const reply = (message) => setImmediate(() => {
        for (const listener of [...listeners]) {
          listener({ data: { requestId: data.requestId, capability: data.capability, ...message }, origin: self.location.origin, source: parent })
        }
      })
      if (data.type === 'moebius:capability-open' && data.input?.operation === 'model-stream') {
        reply({ type: 'moebius:capability-event', event: 'manifest', value: MANIFEST })
      }
      if (data.type === 'moebius:capability-control' && data.action === 'start') {
        reply({ type: 'moebius:capability-event', event: 'chunk', value: { index: 0, assetId: 'model', offset: 0, bytes: new ArrayBuffer(4) } })
      }
      if (data.type === 'moebius:capability-control' && data.action === 'chunk-accepted') {
        reply({ type: 'moebius:capability-result', value: { modelId: 'alba' } })
      }
    },
  }
  const self = {
    location: { origin: 'https://app.mobius.test' },
    parent,
    addEventListener(type, listener) { if (type === 'message') listeners.add(listener) },
    removeEventListener(type, listener) { if (type === 'message') listeners.delete(listener) },
  }
  const capabilities = makeCapabilities({
    declarations: { 'media.speech': { version: 1, limits: { max_text_chars: 1000 } } },
    hostWindow: parent,
    selfWindow: self,
    visibility,
  })
  const speakHere = (text) => capabilities.open('media.speech', { operation: 'synthesize', text, modelId: 'alba' }).result
  return { capabilities, visibility, speakHere }
}

test('the app runtime keeps the engine only while the app is on screen and attached', async () => {
  const host = fakeHost()
  await host.speakHere('One.')
  await host.speakHere('Two.')
  assert.equal(workers.length, 1, 'repeated synthesis reuses one engine')

  host.visibility.set(false)
  assert.equal(workers[0].terminated, true, 'hiding the app releases its idle engine')
  host.visibility.set(true)
  await host.speakHere('Three.')
  assert.equal(workers.length, 2)

  host.capabilities._destroy()
  assert.equal(workers[1].terminated, true, 'detaching the runtime releases the engine')
})
