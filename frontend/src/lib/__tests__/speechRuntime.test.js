import { test } from 'node:test'
import assert from 'node:assert/strict'
import { webcrypto } from 'node:crypto'

import {
  browserSpeechEngine,
  PocketTtsWorkerRuntime,
  releaseBrowserSpeechEngine,
} from '../speech/pocketTtsEngine.js'
import { SPEECH_WORKER_URL } from '../speech/speechWorkerAsset.js'
import {
  SPEECH_PITCH_WORKLET_PATH,
  SPEECH_PITCH_WORKLET_URL,
  SPEECH_PITCH_WORKLET_VERSION,
} from '../speech/speechPitchAsset.js'
import {
  DEFAULT_SPEECH_ENGINE_ID,
  DEFAULT_SPEECH_MODEL_ID,
  publicSpeechEngine,
  publicSpeechModel,
  speechEngine,
  speechModel,
  speechModelLoadSnapshot,
  speechModelPackages,
  speechModels,
  SPEECH_MODEL_STORAGE_LIMITS,
} from '../speech/speechModels.js'
import { POCKET_TTS_V2_ASSETS } from '../speech/speechModelAssets.js'
import {
  removeSpeechModel,
  removeSpeechEngine,
  saveSpeechClone,
  selectSpeechModel,
  speechModelCatalog,
  speechPlaybackCatalog,
} from '../speech/speechModelStore.js'
import { openSpeechModelsCapability } from '../speech/speechModelsProviderRuntime.js'
import { SpeechGenerationOwnership } from '../speech/speechGenerationOwnership.js'
import * as xnRuntime from '../speech/pocketTtsXnRuntime.js'

function runtimeWithWorker() {
  const messages = []
  const runtime = new PocketTtsWorkerRuntime()
  runtime.worker = {
    postMessage(value, transfer = []) { messages.push({ value, transfer }) },
  }
  return { runtime, messages }
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem(key) { return values.get(key) ?? null },
    setItem(key, value) { values.set(key, value) },
    removeItem(key) { values.delete(key) },
  }
}

function voiceSamples(length = 24_000 * 3, amplitude = 0.25) {
  const samples = new Float32Array(length)
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = Math.sin((index + 1) / 20) * amplitude
  }
  return samples
}

function silentPcm16Base64(length = 24_000 * 3) {
  const bytes = new Uint8Array(new Int16Array(length).buffer)
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768))
  }
  return globalThis.btoa(binary)
}

function missingSpeechAssets() {
  return {
    cacheStorage: {
      async keys() { return [] },
      async open() { throw new Error('missing partitions must not be opened') },
    },
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
  }
}

function readySpeechAssetsFor(modelId) {
  const chunks = new Map()
  for (const pkg of speechModelPackages(speechModel(modelId))) {
    for (const asset of pkg.assets) {
      asset.chunks.forEach((chunk, index) => {
        chunks.set(`${pkg.key}/${asset.id}/${index}`, chunk)
      })
    }
  }
  const cache = {
    async match(value) {
      const url = typeof value === 'string' ? value : value.url
      const path = new URL(url).pathname.split('/').map(decodeURIComponent)
      const chunk = chunks.get(`${path.at(-4)}/${path.at(-2)}/${path.at(-1)}`)
      if (!chunk) return undefined
      return new Response('', {
        headers: {
          'Content-Length': String(chunk.bytes),
          'X-Mobius-SHA256': chunk.sha256,
        },
      })
    },
    async delete() { throw new Error('valid cached chunks must not be deleted') },
  }
  return {
    cacheStorage: {
      async keys() { return ['mobius-device-assets-v1-speech-v2'] },
      async open() { return cache },
    },
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
  }
}

function runSpeechModelsOperation(input, storage) {
  return new Promise((resolve, reject) => {
    openSpeechModelsCapability({
      appId: 61,
      input,
      declaration: { limits: SPEECH_MODEL_STORAGE_LIMITS },
      storage,
      channel: { ready() {}, event() {}, result: resolve, error: reject },
    })
  })
}

test('a frame reader receives one model chunk at a time', async () => {
  const events = []
  let firstChunk
  let secondChunk
  const receivedFirst = new Promise((resolve) => { firstChunk = resolve })
  const receivedSecond = new Promise((resolve) => { secondChunk = resolve })
  let control
  const result = new Promise((resolve, reject) => {
    control = openSpeechModelsCapability({
      appId: 61,
      input: { operation: 'read', engineId: DEFAULT_SPEECH_ENGINE_ID },
      declaration: { limits: SPEECH_MODEL_STORAGE_LIMITS },
      dependencies: readySpeechAssetsFor(DEFAULT_SPEECH_MODEL_ID),
      channel: {
        ready() {},
        event(name, value) {
          if (name === 'manifest') control.control('start')
          if (name !== 'chunk') return
          events.push(value)
          if (events.length === 1) firstChunk()
          if (events.length === 2) secondChunk()
        },
        result: resolve,
        error: reject,
      },
    })
  })

  await receivedFirst
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(events.length, 1)

  control.control('chunk-accepted')
  await receivedSecond
  assert.equal(events.length, 2)

  control.control('cancel')
  await assert.rejects(result, (error) => error.name === 'AbortError')
})

test('speech model selection exposes only the public catalog contract', () => {
  const selected = speechModel(DEFAULT_SPEECH_MODEL_ID)
  assert.equal(selected.id, 'pocket-tts-alba')
  assert.equal(selected.engineId, DEFAULT_SPEECH_ENGINE_ID)
  assert.deepEqual(speechModels().map((model) => model.voice), [
    'Alba', 'Azelma', 'Cosette', 'Eponine', 'Fantine', 'Javert', 'Jean', 'Marius',
    'Jürgen', 'Giovanni', 'Rafael', 'Lola',
  ])
  assert.equal(speechModel('missing'), null)
  assert.deepEqual(Object.keys(publicSpeechModel(selected)).sort(), [
    'cloned', 'description', 'engine', 'engineId', 'id', 'language', 'name', 'profileBytes',
    'sampleRate', 'sharedBytes', 'storedBytes', 'voice',
  ])
  assert.deepEqual(publicSpeechEngine(speechEngine(DEFAULT_SPEECH_ENGINE_ID)), {
    id: DEFAULT_SPEECH_ENGINE_ID,
    name: 'Pocket TTS · English',
    delivery: 'device',
    languages: ['English'],
    storedBytes: 148_302_091,
  })
})

test('speech models download from the public versioned Voice release', () => {
  for (const [file, asset] of Object.entries(POCKET_TTS_V2_ASSETS)) {
    assert.equal(
      asset.url,
      `https://github.com/mobius-os/app-voice/releases/download/models-v2/${file}`,
    )
  }
})

test('a recorded clone is resampled, persisted privately, and becomes a catalog model', () => {
  const storage = memoryStorage()
  const source = voiceSamples(48_000 * 3)
  const saved = saveSpeechClone({
    language: 'German', name: 'My voice', samples: source, sampleRate: 48_000,
  }, { storage })

  assert.equal(saved.id, 'pocket-tts-clone-german')
  assert.equal(saved.cloned, true)
  const restored = speechModelLoadSnapshot(saved.id, storage).clonedVoiceSamples
  assert.equal(restored.length, 24_000 * 3)
  assert.ok(Math.abs(restored[100] - source[200]) < 0.001)
  assert.equal(storage.getItem('mobius:speech:active-model:v1'), saved.id)
})

test('clone load identity is deterministic for its recording content', () => {
  const samples = voiceSamples()
  const firstStorage = memoryStorage()
  const secondStorage = memoryStorage()
  const first = saveSpeechClone({
    language: 'English', name: 'First', samples, sampleRate: 24_000,
  }, { storage: firstStorage })
  const second = saveSpeechClone({
    language: 'English', name: 'Renamed', samples, sampleRate: 24_000,
  }, { storage: secondStorage })
  const firstLibrary = JSON.parse(firstStorage.getItem('mobius:speech:cloned-profiles:v1'))
  const secondLibrary = JSON.parse(secondStorage.getItem('mobius:speech:cloned-profiles:v1'))
  firstLibrary[0].revision = 'old-random-revision-a'
  secondLibrary[0].revision = 'old-random-revision-b'
  firstStorage.setItem('mobius:speech:cloned-profiles:v1', JSON.stringify(firstLibrary))
  secondStorage.setItem('mobius:speech:cloned-profiles:v1', JSON.stringify(secondLibrary))
  const persistedFirst = firstStorage.getItem('mobius:speech:cloned-profiles:v1')

  assert.equal(
    speechModelLoadSnapshot(first.id, firstStorage).identity,
    speechModelLoadSnapshot(second.id, secondStorage).identity,
  )
  assert.equal(firstStorage.getItem('mobius:speech:cloned-profiles:v1'), persistedFirst)
})

test('invalid legacy clone revision metadata cannot wedge the voice library', () => {
  const storage = memoryStorage()
  const samples = voiceSamples()
  const saved = saveSpeechClone({
    language: 'English', name: 'Original', samples, sampleRate: 24_000,
  }, { storage })
  const library = JSON.parse(storage.getItem('mobius:speech:cloned-profiles:v1'))
  library[0].revision = { obsolete: true }
  storage.setItem('mobius:speech:cloned-profiles:v1', JSON.stringify(library))

  assert.ok(speechModelLoadSnapshot(saved.id, storage))
  saveSpeechClone({
    language: 'English', name: 'Updated', samples, sampleRate: 24_000,
  }, { storage })
  const rewritten = JSON.parse(storage.getItem('mobius:speech:cloned-profiles:v1'))
  assert.equal('revision' in rewritten[0], false)
})

test('replacing a same-language clone changes its content identity and reloads the engine', async (t) => {
  const storage = memoryStorage()
  const firstSamples = voiceSamples(24_000 * 3, 0.1)
  const secondSamples = voiceSamples(24_000 * 3, 0.5)
  const first = saveSpeechClone({
    language: 'English', name: 'First', samples: firstSamples, sampleRate: 24_000,
  }, { storage })
  const firstSnapshot = speechModelLoadSnapshot(first.id, storage)

  releaseBrowserSpeechEngine()
  t.after(() => releaseBrowserSpeechEngine())
  const engine = browserSpeechEngine()
  const loads = []
  let disposals = 0
  engine.runtime = {
    async load({ snapshot }) {
      loads.push({
        identity: snapshot.identity,
        sample: snapshot.clonedVoiceSamples[0],
      })
    },
    dispose() { disposals += 1 },
  }
  await engine.load({ modelId: first.id, storage })

  const replacement = saveSpeechClone({
    language: 'English', name: 'Second', samples: secondSamples, sampleRate: 24_000,
  }, { storage })
  const replacementSnapshot = speechModelLoadSnapshot(replacement.id, storage)
  await engine.load({ modelId: replacement.id, storage })

  assert.equal(replacement.id, first.id)
  assert.notEqual(replacementSnapshot.identity, firstSnapshot.identity)
  assert.deepEqual(loads.map((value) => value.identity), [
    firstSnapshot.identity, replacementSnapshot.identity,
  ])
  assert.ok(loads[0].sample < loads[1].sample)
  assert.equal(disposals, 1)
})

test('clone persistence trims oversized input to eight seconds before resampling', () => {
  const storage = memoryStorage()
  const source = voiceSamples(48_000 * 9)
  source.fill(0.75, 48_000 * 8)
  const saved = saveSpeechClone({
    language: 'English', name: 'Trimmed', samples: source, sampleRate: 48_000,
  }, { storage })

  const restored = speechModelLoadSnapshot(saved.id, storage).clonedVoiceSamples
  assert.equal(restored.length, 24_000 * 8)
  assert.ok(Math.abs(restored.at(-1) - source[48_000 * 8 - 2]) < 0.001)
})

test('silent or malformed clone recordings are rejected before storage changes', () => {
  const storage = memoryStorage()
  assert.throws(
    () => saveSpeechClone({
      language: 'English', name: 'Silent', samples: new Float32Array(24_000 * 3), sampleRate: 24_000,
    }, { storage }),
    (error) => error.code === 'invalid_request' && error.name === 'TypeError',
  )
  assert.throws(
    () => saveSpeechClone({
      language: 'English', name: 'Flat signal',
      samples: new Float32Array(24_000 * 3).fill(0.25), sampleRate: 24_000,
    }, { storage }),
    (error) => error.code === 'invalid_request' && error.name === 'TypeError',
  )
  const malformed = voiceSamples()
  malformed[0] = Number.NaN
  assert.throws(
    () => saveSpeechClone({
      language: 'English', name: 'Malformed', samples: malformed, sampleRate: 24_000,
    }, { storage }),
    (error) => error.code === 'invalid_request' && error.name === 'TypeError',
  )
  assert.equal(storage.getItem('mobius:speech:cloned-profiles:v1'), null)
})

test('a legacy silent clone cannot reach the speech worker', () => {
  const storage = memoryStorage()
  const saved = saveSpeechClone({
    language: 'English', name: 'Old clone', samples: voiceSamples(), sampleRate: 24_000,
  }, { storage })
  const library = JSON.parse(storage.getItem('mobius:speech:cloned-profiles:v1'))
  library[0].pcm16Base64 = silentPcm16Base64()
  storage.setItem('mobius:speech:cloned-profiles:v1', JSON.stringify(library))

  assert.ok(speechModel(saved.id, storage))
  assert.throws(
    () => speechModelLoadSnapshot(saved.id, storage),
    (error) => error.code === 'silent_recording',
  )
})

test('clone saves do not re-read storage after the write succeeds', () => {
  let libraryReads = 0
  let savedLibrary = null
  const storage = {
    getItem(key) {
      if (key !== 'mobius:speech:cloned-profiles:v1') return null
      libraryReads += 1
      if (libraryReads > 1) throw new Error('post-write reads are unsafe')
      return null
    },
    setItem(key, value) {
      if (key === 'mobius:speech:cloned-profiles:v1') savedLibrary = value
    },
  }

  saveSpeechClone({
    language: 'English', name: 'Saved once',
    samples: voiceSamples(), sampleRate: 24_000,
  }, { storage })

  assert.equal(libraryReads, 1)
  assert.equal(JSON.parse(savedLibrary)[0].name, 'Saved once')
})

test('clone persistence preserves a damaged library instead of overwriting it', () => {
  const damaged = '{not valid json'
  const storage = memoryStorage({ 'mobius:speech:cloned-profiles:v1': damaged })
  assert.throws(
    () => saveSpeechClone({
      language: 'English', name: 'Replacement',
      samples: voiceSamples(), sampleRate: 24_000,
    }, { storage }),
    (error) => error.code === 'storage_corrupt',
  )
  assert.equal(storage.getItem('mobius:speech:cloned-profiles:v1'), damaged)
})

test('speech-model catalog reports clone-library readiness without repairing stored data', async () => {
  const damaged = '{not valid json'
  const readyStorage = memoryStorage()
  const damagedStorage = memoryStorage({ 'mobius:speech:cloned-profiles:v1': damaged })
  const unavailableStorage = {
    getItem() { throw new Error('storage denied') },
  }
  const dependencies = missingSpeechAssets()

  const [ready, broken, unavailable] = await Promise.all([
    speechModelCatalog({ storage: readyStorage, dependencies }),
    speechModelCatalog({ storage: damagedStorage, dependencies }),
    speechModelCatalog({ storage: unavailableStorage, dependencies }),
  ])

  assert.deepEqual(ready.cloneLibrary, { status: 'ready' })
  assert.deepEqual(broken.cloneLibrary, { status: 'damaged' })
  assert.deepEqual(unavailable.cloneLibrary, { status: 'unavailable' })
  assert.equal(damagedStorage.getItem('mobius:speech:cloned-profiles:v1'), damaged)
})

test('clone-library reset refuses to touch damaged data without exact confirmation', async () => {
  const damaged = '{not valid json'
  const storage = memoryStorage({ 'mobius:speech:cloned-profiles:v1': damaged })

  await assert.rejects(
    runSpeechModelsOperation({ operation: 'reset-clones', confirm: 'true' }, storage),
    (error) => error.code === 'confirmation_required',
  )
  assert.equal(storage.getItem('mobius:speech:cloned-profiles:v1'), damaged)
})

test('confirmed clone-library reset removes only clone data and an active clone selection', async () => {
  const cloneKey = 'mobius:speech:cloned-profiles:v1'
  const activeKey = 'mobius:speech:active-model:v1'
  const values = new Map([
    [cloneKey, '{damaged'],
    [activeKey, 'pocket-tts-clone-english'],
    ['unrelated', 'preserve me'],
  ])
  const removed = []
  const storage = {
    getItem(key) { return values.get(key) ?? null },
    removeItem(key) { removed.push(key); values.delete(key) },
  }

  const result = await runSpeechModelsOperation({ operation: 'reset-clones', confirm: true }, storage)

  assert.deepEqual(result, { cloneLibrary: { status: 'ready' } })
  assert.deepEqual(removed, [activeKey, cloneKey])
  assert.equal(values.get('unrelated'), 'preserve me')
})

test('clone-library reset reports unavailable storage without losing corrupt data', async () => {
  const cloneKey = 'mobius:speech:cloned-profiles:v1'
  const activeKey = 'mobius:speech:active-model:v1'
  const damaged = '{not valid json'
  const values = new Map([
    [cloneKey, damaged],
    [activeKey, 'pocket-tts-clone-english'],
  ])
  const storage = {
    getItem(key) { return values.get(key) ?? null },
    removeItem(key) {
      if (key === cloneKey) throw new Error('storage denied')
      values.delete(key)
    },
  }

  await assert.rejects(
    runSpeechModelsOperation({ operation: 'reset-clones', confirm: true }, storage),
    (error) => error.code === 'storage_unavailable',
  )
  assert.equal(values.get(cloneKey), damaged)
  assert.equal(values.has(activeKey), false)
})

test('speech playback catalog projects only the active ready voice', async () => {
  const active = await speechPlaybackCatalog({
    storage: memoryStorage(),
    dependencies: readySpeechAssetsFor(DEFAULT_SPEECH_MODEL_ID),
  })
  const missing = await speechPlaybackCatalog({
    storage: memoryStorage(),
    dependencies: missingSpeechAssets(),
  })

  assert.deepEqual(active, {
    activeModel: {
      id: DEFAULT_SPEECH_MODEL_ID,
      name: 'Alba',
      language: 'English',
      sampleRate: 24_000,
    },
    playback: {
      pitchPreserving: true,
      workletUrl: SPEECH_PITCH_WORKLET_URL,
    },
  })
  assert.deepEqual(Object.keys(active), ['activeModel', 'playback'])
  assert.equal('engines' in active, false)
  assert.equal('models' in active, false)
  assert.deepEqual(missing, {
    activeModel: null,
    playback: {
      pitchPreserving: true,
      workletUrl: SPEECH_PITCH_WORKLET_URL,
    },
  })
})

test('pitch-preserving speech worklet registers and compensates a faster source', async (t) => {
  const OriginalProcessor = globalThis.AudioWorkletProcessor
  const originalRegister = globalThis.registerProcessor
  const originalSampleRate = globalThis.sampleRate
  const messages = []
  let registration
  globalThis.sampleRate = 48_000
  globalThis.AudioWorkletProcessor = class {
    constructor() {
      this.port = { postMessage: message => messages.push(message) }
    }
  }
  globalThis.registerProcessor = (name, Processor) => { registration = { name, Processor } }
  t.after(() => {
    globalThis.AudioWorkletProcessor = OriginalProcessor
    globalThis.registerProcessor = originalRegister
    if (originalSampleRate === undefined) delete globalThis.sampleRate
    else globalThis.sampleRate = originalSampleRate
  })

  await import(new URL(`../../../public/${SPEECH_PITCH_WORKLET_PATH}`, import.meta.url))

  assert.equal(SPEECH_PITCH_WORKLET_VERSION, '2.1.1')
  assert.equal(registration.name, 'soundtouch-processor')
  assert.equal(typeof registration.Processor, 'function')

  const processor = new registration.Processor({
    processorOptions: { sampleBufferType: 'circular' },
  })
  const rendered = []
  let phase = 0
  const sourceStep = 2 * Math.PI * 660 / globalThis.sampleRate
  for (let block = 0; block < 750; block += 1) {
    const input = new Float32Array(128)
    for (let frame = 0; frame < input.length; frame += 1) {
      input[frame] = Math.sin(phase) * 0.25
      phase += sourceStep
    }
    const output = new Float32Array(128)
    processor.process([[input]], [[output]], {
      pitch: Float32Array.of(1),
      pitchSemitones: Float32Array.of(0),
      playbackRate: Float32Array.of(1.5),
    })
    rendered.push(...output)
  }

  const firstAudible = rendered.findIndex(sample => Math.abs(sample) > 0.02)
  assert.ok(firstAudible >= 0)
  let positiveCrossings = 0
  let previous = rendered[firstAudible]
  for (let index = firstAudible + 1; index < rendered.length; index += 1) {
    const sample = rendered[index]
    if (previous <= 0 && sample > 0) positiveCrossings += 1
    previous = sample
  }
  const audibleSeconds = (rendered.length - firstAudible) / globalThis.sampleRate
  const restoredFrequency = positiveCrossings / audibleSeconds
  assert.ok(Math.abs(restoredFrequency - 440) < 2,
    `expected 440 Hz after pitch compensation, received ${restoredFrequency.toFixed(2)} Hz`)

  const metrics = messages.filter(message => message.type === 'metrics')
  assert.ok(metrics.length >= 2)
  assert.equal(metrics.at(-1).underrunCount, metrics.at(-2).underrunCount,
    'the real processor must stop underrunning after its bounded warm-up')
})

test('clone removal uses the same injected storage that supplied the model', async () => {
  const storage = memoryStorage()
  const saved = saveSpeechClone({
    language: 'Italian', name: 'Temporary',
    samples: voiceSamples(), sampleRate: 24_000,
  }, { storage })

  await removeSpeechModel(saved.id, { storage })

  assert.equal(speechModel(saved.id, storage), null)
  assert.equal(storage.getItem('mobius:speech:active-model:v1'), null)
})

test('clone removal does not verify after storage has already mutated', async () => {
  const seed = memoryStorage()
  const saved = saveSpeechClone({
    language: 'Italian', name: 'Temporary',
    samples: voiceSamples(), sampleRate: 24_000,
  }, { storage: seed })
  let library = seed.getItem('mobius:speech:cloned-profiles:v1')
  let mutated = false
  const storage = {
    getItem(key) {
      if (mutated) throw new Error('post-delete reads are unsafe')
      return key === 'mobius:speech:cloned-profiles:v1' ? library : saved.id
    },
    setItem(key, value) {
      if (key === 'mobius:speech:cloned-profiles:v1') {
        library = value
        mutated = true
      }
    },
    removeItem() {},
  }

  await removeSpeechModel(saved.id, { storage })

  assert.deepEqual(JSON.parse(library), [])
})

test('engine load resolves one coherent clone storage snapshot', async (t) => {
  const firstStorage = memoryStorage()
  const secondStorage = memoryStorage()
  const first = saveSpeechClone({
    language: 'English', name: 'First',
    samples: voiceSamples(24_000 * 3, 0.1), sampleRate: 24_000,
  }, { storage: firstStorage })
  saveSpeechClone({
    language: 'English', name: 'Second',
    samples: voiceSamples(24_000 * 3, 0.7), sampleRate: 24_000,
  }, { storage: secondStorage })
  const libraries = [
    firstStorage.getItem('mobius:speech:cloned-profiles:v1'),
    secondStorage.getItem('mobius:speech:cloned-profiles:v1'),
  ]
  let reads = 0
  const changingStorage = {
    getItem(key) {
      if (key !== 'mobius:speech:cloned-profiles:v1') return null
      return libraries[Math.min(reads++, libraries.length - 1)]
    },
  }

  releaseBrowserSpeechEngine()
  t.after(() => releaseBrowserSpeechEngine())
  const engine = browserSpeechEngine()
  let received = null
  engine.runtime = {
    async load({ snapshot }) { received = snapshot },
    dispose() {},
  }

  await engine.load({ modelId: first.id, storage: changingStorage })

  assert.equal(reads, 1)
  assert.ok(received.clonedVoiceSamples[0] < 0.2)
  assert.equal(received.packages.length, 1)
})

test('voices reuse their language engine and keep profile manifests distinct', () => {
  const available = speechModels()
  const packagesByEngine = new Map()
  for (const model of available) {
    const [shared, profile] = speechModelPackages(model)
    if (packagesByEngine.has(model.engineId)) assert.equal(shared, packagesByEngine.get(model.engineId))
    else packagesByEngine.set(model.engineId, shared)
    assert.match(profile.key, new RegExp(model.id.replace('pocket-tts-', '')))
    assert.deepEqual(speechModelLoadSnapshot(model.id).assetBytes, {
      tokenizer: shared.assets.find((asset) => asset.id === 'tokenizer').bytes,
      model: 148_242_752,
      voice: model.profileBytes,
    })
  }
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
  assert.deepEqual(catalog.engines.map(({ languages, state }) => ({ languages, state })), [
    { languages: ['English'], state: 'missing' },
    { languages: ['German'], state: 'missing' },
    { languages: ['Italian'], state: 'missing' },
    { languages: ['Portuguese'], state: 'missing' },
    { languages: ['Spanish'], state: 'missing' },
  ])
  assert.equal(catalog.models.length, 12)
  assert.ok(catalog.models.every((model) => model.state === 'missing'))
  assert.ok(catalog.models.every((model) => model.profileState === 'missing'))
  await assert.rejects(
    selectSpeechModel(DEFAULT_SPEECH_MODEL_ID, options),
    (error) => error.code === 'not_installed',
  )
})

test('removing a language engine clears its active voice and remains safe when already absent', async () => {
  const storage = memoryStorage({ 'mobius:speech:active-model:v1': DEFAULT_SPEECH_MODEL_ID })
  const result = await removeSpeechEngine(DEFAULT_SPEECH_ENGINE_ID, {
    storage,
    declaration: { limits: SPEECH_MODEL_STORAGE_LIMITS },
    dependencies: {
      cacheStorage: {
        async keys() { return [] },
        async open() { throw new Error('an absent partition must not be opened') },
      },
      cryptoImpl: webcrypto,
      origin: 'https://mobius.test',
      storageManager: {},
    },
  })

  assert.equal(result.state, 'missing')
  assert.equal(storage.getItem('mobius:speech:active-model:v1'), null)
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

test('the XN runtime exposes the streaming model allocation it was adapted for', () => {
  assert.equal(typeof xnRuntime.allocate_model_weights, 'function')
  assert.equal(typeof xnRuntime.model_from_allocated_weights, 'function')
  assert.equal(typeof xnRuntime.free_allocated_model_weights, 'function')
  assert.equal(typeof xnRuntime.Model.fromRawModelWeights, 'function')
})

// The engine must load its worker from a served same-origin URL. Building one
// from a blob: URL instead would need `blob:` in the shell's script-src, and
// without it the worker never loads at all.
test('the engine starts its worker from the served same-origin asset', (t) => {
  const OriginalWorker = globalThis.Worker
  const urls = []
  globalThis.Worker = class FakeWorker {
    constructor(url) { urls.push(url) }
    postMessage() {}
    terminate() {}
  }
  t.after(() => { globalThis.Worker = OriginalWorker })

  const runtime = new PocketTtsWorkerRuntime()
  t.after(() => runtime.dispose())
  runtime.ensureWorker()

  assert.deepEqual(urls, ['/speech/pocket-tts-worker.js'])
})

test('a worker that never loaded is reported as unavailable, not as memory pressure', async (t) => {
  const OriginalWorker = globalThis.Worker
  globalThis.Worker = class FakeWorker {
    postMessage() {}
    terminate() {}
  }
  t.after(() => { globalThis.Worker = OriginalWorker })

  const runtime = new PocketTtsWorkerRuntime()
  t.after(() => runtime.dispose())
  const worker = runtime.ensureWorker()
  const pending = runtime.generate('Hello')
  // A script that never ran fires a bare Event: no message, no error object.
  worker.onerror({})

  await assert.rejects(pending, (error) => (
    error.message.includes(SPEECH_WORKER_URL)
    && /security policy/.test(error.message)
    && !/close other tabs/i.test(error.message)
  ))
})

test('worker generation forwards audio and settles only its matching request', async () => {
  const { runtime, messages } = runtimeWithWorker()
  const audio = []
  const result = runtime.generate('Hello', { onChunkSync: (samples) => audio.push(samples) })
  const requestId = messages[0].value.requestId
  const samples = new Float32Array([0.25])
  runtime.onMessage({ type: 'audio', requestId, samples })
  runtime.onMessage({ type: 'generate-complete', requestId: 'another-request' })
  await Promise.resolve()
  assert.deepEqual(audio, [samples])
  runtime.onMessage({ type: 'generate-complete', requestId })
  await result
})

test('a synchronous audio consumer failure cancels and settles worker generation', async () => {
  const { runtime, messages } = runtimeWithWorker()
  const result = runtime.generate('Hello', {
    onChunkSync() { throw new Error('audio consumer failed') },
  })
  const requestId = messages[0].value.requestId

  runtime.onMessage({ type: 'audio', requestId, samples: new Float32Array([0.25]) })

  await assert.rejects(result, /audio consumer failed/)
  assert.equal(runtime.generations.size, 0)
  assert.equal(messages.at(-1).value.type, 'cancel-generate')
})

test('synchronous audio delivery does not inspect asynchronous return values', async () => {
  const { runtime, messages } = runtimeWithWorker()
  const result = runtime.generate('Hello', {
    onChunkSync() {
      return { then() { throw new Error('async return value was inspected') } }
    },
  })
  const requestId = messages[0].value.requestId

  runtime.onMessage({ type: 'audio', requestId, samples: new Float32Array([0.25]) })
  await Promise.resolve()

  assert.equal(runtime.generations.has(requestId), true)
  runtime.onMessage({ type: 'generate-complete', requestId })
  await result
  assert.notEqual(messages.at(-1).value.type, 'cancel-generate')
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

test('an already-aborted load never starts the speech worker', async () => {
  const runtime = new PocketTtsWorkerRuntime()
  const controller = new AbortController()
  let workerStarted = false
  runtime.ensureWorker = () => { workerStarted = true }
  controller.abort()

  await assert.rejects(
    runtime.load({ signal: controller.signal }),
    (error) => error.name === 'AbortError',
  )
  assert.equal(workerStarted, false)
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

test('a stopped worker is retired so the next attempt starts a fresh worker', async (t) => {
  const OriginalWorker = globalThis.Worker
  const workers = []
  globalThis.Worker = class FakeWorker {
    constructor() { this.terminated = false; workers.push(this) }
    postMessage() {}
    terminate() { this.terminated = true }
  }
  t.after(() => { globalThis.Worker = OriginalWorker })

  const runtime = new PocketTtsWorkerRuntime()
  t.after(() => runtime.dispose())
  const first = runtime.ensureWorker()
  const pending = runtime.generate('Hello')
  first.onerror({ message: 'the speech worker crashed' })

  await assert.rejects(pending, /the speech worker crashed/)
  assert.equal(first.terminated, true)
  assert.equal(runtime.worker, null)
  const second = runtime.ensureWorker()
  assert.notEqual(second, first)
  assert.equal(workers.length, 2)
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
