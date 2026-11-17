import { test, beforeEach, afterEach, mock } from 'node:test'
import assert from 'node:assert/strict'
import {
  releaseSpeechEngine, retainSpeechEngine, synthesizeInFrame,
} from '../../runtime/speech.js'
import { makeCapabilities } from '../../runtime/capabilities.js'
import { synthesizeSpeech } from '../speech/speechProviderRuntime.js'
import { browserSpeechEngine, releaseBrowserSpeechEngine } from '../speech/pocketTtsEngine.js'
import {
  DEFAULT_SPEECH_MODEL_ID, speechModel, speechModelPackages,
} from '../speech/speechModels.js'

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

test('a parked engine is released after an idle delay unless reused', async (t) => {
  mock.timers.enable({ apis: ['setTimeout'] })
  t.after(() => mock.timers.reset())

  await speak({ text: 'One.', modelId: 'alba' }).session.result
  mock.timers.tick(59_000)
  assert.equal(workers[0].terminated, false)
  await speak({ text: 'Two.', modelId: 'alba' }).session.result
  assert.equal(workers.length, 1, 'a request within the delay reuses the engine')

  // Reuse restarted the delay: the first park's deadline has passed, the
  // second's has not.
  mock.timers.tick(59_000)
  assert.equal(workers[0].terminated, false)
  mock.timers.tick(1_000)
  assert.equal(workers[0].terminated, true, 'an idle engine gives its memory back')

  await speak({ text: 'Three.', modelId: 'alba' }).session.result
  assert.equal(workers.length, 2, 'the next request starts a fresh engine')
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

  // The next session is still generating when the cancelled request's last
  // replies arrive.
  const second = speak({ text: 'hold', modelId: 'alba' })
  await until(() => workers[0].held !== cancelledRequest)
  workers[0].reply({ type: 'audio', requestId: cancelledRequest, samples: new Float32Array([0.9]) })
  workers[0].reply({ type: 'generate-complete', requestId: cancelledRequest })
  await tick()
  await tick()
  workers[0].reply({ type: 'generate-complete', requestId: workers[0].held })
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

// Every chunk of the default voice, as the shell's device-asset cache holds it
// once the voice is downloaded.
function downloadedDefaultVoice() {
  const chunks = new Map()
  for (const pkg of speechModelPackages(speechModel(DEFAULT_SPEECH_MODEL_ID))) {
    for (const asset of pkg.assets) {
      asset.chunks.forEach((chunk, index) => chunks.set(`${pkg.key}/${asset.id}/${index}`, chunk))
    }
  }
  const cache = {
    async match(value) {
      const path = new URL(typeof value === 'string' ? value : value.url).pathname.split('/')
      const chunk = chunks.get(`${path.at(-4)}/${path.at(-2)}/${path.at(-1)}`)
      return chunk && new Response('', {
        headers: { 'Content-Length': String(chunk.bytes), 'X-Mobius-SHA256': chunk.sha256 },
      })
    },
  }
  return { async keys() { return ['mobius-device-assets-v1-speech-v2'] }, async open() { return cache } }
}

test('the shell engine path reports one boundary per caller segment too', async (t) => {
  const values = new Map()
  const globals = ['caches', 'localStorage'].map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)])
  const define = (name, value) => Object.defineProperty(globalThis, name, { value, configurable: true, writable: true })
  define('caches', downloadedDefaultVoice())
  define('localStorage', {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  })
  globalThis.location = { href: 'https://mobius.test/', origin: 'https://mobius.test' }
  releaseBrowserSpeechEngine()
  t.after(() => {
    releaseBrowserSpeechEngine()
    for (const [name, descriptor] of globals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  })
  const engine = browserSpeechEngine()
  const generated = []
  engine.load = async () => {}
  engine.generate = async (text, { onChunk }) => {
    generated.push(text)
    onChunk(new Float32Array([0.1]))
  }

  const long = Array.from({ length: 12 }, (_, i) => `Sentence number ${i} carries some words.`).join(' ')
  assert.ok(long.length > 200)
  const boundaries = []
  const metadata = await synthesizeSpeech({
    document: { version: 1, segments: [{ text: long, kind: 'paragraph', pauseAfterMs: 700 }] },
    onAudio() {},
    onBoundary: (value) => boundaries.push(value),
  }).result

  assert.ok(generated.length > 1, 'the long block was generated in pieces')
  assert.equal(metadata.segmentCount, generated.length)
  assert.deepEqual(boundaries, [{ segmentIndex: 0, kind: 'paragraph', pauseAfterMs: 700 }])
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
    declarations: SPEECH_DECLARED,
    hostWindow: parent,
    selfWindow: self,
    visibility,
  })
  const start = (text) => capabilities.open('media.speech', { operation: 'synthesize', text, modelId: 'alba' })
  const speakHere = (text) => start(text).result
  return { capabilities, visibility, start, speakHere }
}

const SPEECH_DECLARED = Object.freeze({ 'media.speech': { version: 1, limits: { max_text_chars: 1000 } } })

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

test('detaching the runtime stops a session that is still speaking', async () => {
  const host = fakeHost()
  const speaking = host.start('hold')
  await until(() => workers[0]?.held)
  host.capabilities._destroy()

  await assert.rejects(speaking.result, { name: 'AbortError' })
  assert.equal(workers[0].messages.at(-1).type, 'cancel-generate')
  assert.equal(workers[0].terminated, true)
})

test('an app that stops declaring speech keeps no engine, even one still speaking', async () => {
  const host = fakeHost()

  // Removed while generating, then the request completes.
  const completing = host.start('hold')
  await until(() => workers[0]?.held)
  host.capabilities._updateDeclarations({})
  workers[0].reply({ type: 'generate-complete', requestId: workers[0].held })
  await completing.result
  assert.equal(workers[0].terminated, true, 'a completed request does not park its engine')

  // Removed while generating, then the request is cancelled.
  host.capabilities._updateDeclarations(SPEECH_DECLARED)
  const cancelled = host.start('hold')
  await until(() => workers[1]?.held)
  host.capabilities._updateDeclarations({})
  cancelled.cancel()
  await assert.rejects(cancelled.result, { name: 'AbortError' })
  assert.equal(workers[1].terminated, true, 'a cancelled request does not park its engine')

  // Showing the frame again does not bring retention back while undeclared.
  host.capabilities._updateDeclarations(SPEECH_DECLARED)
  const shown = host.start('hold')
  await until(() => workers[2]?.held)
  host.capabilities._updateDeclarations({})
  host.visibility.set(false)
  host.visibility.set(true)
  workers[2].reply({ type: 'generate-complete', requestId: workers[2].held })
  await shown.result
  assert.equal(workers[2].terminated, true, 'visibility alone does not keep an undeclared engine')

  // Declaring speech again restores reuse.
  host.capabilities._updateDeclarations(SPEECH_DECLARED)
  await host.speakHere('One.')
  await host.speakHere('Two.')
  assert.equal(workers.length, 4)
  assert.equal(workers[3].terminated, false)
  host.capabilities._destroy()
})
