import { POCKET_TTS_V2_ASSETS } from './speechModelAssets.js'

const LANGUAGE_SPECS = Object.freeze([
  Object.freeze({ id: 'english', label: 'English', voice: 'alba', voiceLabel: 'Alba' }),
  Object.freeze({ id: 'german', label: 'German', voice: 'juergen', voiceLabel: 'Jürgen' }),
  Object.freeze({ id: 'italian', label: 'Italian', voice: 'giovanni', voiceLabel: 'Giovanni' }),
  Object.freeze({ id: 'portuguese', label: 'Portuguese', voice: 'rafael', voiceLabel: 'Rafael' }),
  Object.freeze({ id: 'spanish', label: 'Spanish', voice: 'lola', voiceLabel: 'Lola' }),
])

const ENGLISH_VOICES = Object.freeze([
  ['alba', 'Alba'], ['azelma', 'Azelma'], ['cosette', 'Cosette'], ['eponine', 'Eponine'],
  ['fantine', 'Fantine'], ['javert', 'Javert'], ['jean', 'Jean'], ['marius', 'Marius'],
])
const CLONED_PROFILES_KEY = 'mobius:speech:cloned-profiles:v1'
const MIN_CLONE_PCM_BYTES = 24_000 * 3 * 2
const MAX_CLONE_PCM_BYTES = 24_000 * 8 * 2

function asset(file, id) {
  const value = POCKET_TTS_V2_ASSETS[file]
  if (!value) throw new Error(`Missing speech asset: ${file}`)
  return Object.freeze({ id, ...value })
}

function enginePackage(language) {
  return Object.freeze({
    key: `pocket-tts-v2-${language.id}-q8-cloning-engine-v1`,
    assets: Object.freeze([
      asset(`${language.id}-tokenizer.model`, 'tokenizer'),
      asset(`${language.id}-q8.gguf`, 'model'),
    ]),
  })
}

function profilePackage(languageId, voiceId) {
  return Object.freeze({
    key: `pocket-tts-v2-${languageId}-${voiceId}-profile-v1`,
    assets: Object.freeze([asset(`${languageId}-${voiceId}.safetensors`, 'voice')]),
  })
}

export const SPEECH_MODEL_PARTITION = 'speech-v2'
export const SPEECH_MODEL_STORAGE_LIMITS = Object.freeze({
  max_bytes: 768 * 1024 * 1024,
  max_asset_bytes: 256 * 1024 * 1024,
  max_chunk_bytes: 8 * 1024 * 1024,
})

const engines = Object.freeze(LANGUAGE_SPECS.map((language) => {
  const pkg = enginePackage(language)
  return Object.freeze({
    id: `pocket-tts-xn-q8-${language.id}`,
    name: `Pocket TTS · ${language.label}`,
    delivery: 'device',
    languages: Object.freeze([language.label]),
    storedBytes: pkg.assets.reduce((total, item) => total + item.bytes, 0),
    package: pkg,
  })
}))

export const DEFAULT_SPEECH_ENGINE_ID = engines[0].id

const voiceSpecs = Object.freeze([
  ...ENGLISH_VOICES.map(([id, label]) => ({ languageId: 'english', language: 'English', id, label })),
  ...LANGUAGE_SPECS.slice(1).map((item) => ({
    languageId: item.id, language: item.label, id: item.voice, label: item.voiceLabel,
  })),
])

const builtInModels = Object.freeze(voiceSpecs.map((voice) => {
  const engine = engines.find((item) => item.languages[0] === voice.language)
  const profile = profilePackage(voice.languageId, voice.id)
  const profileBytes = profile.assets[0].bytes
  return Object.freeze({
    id: `pocket-tts-${voice.id}`,
    name: voice.label,
    engineId: engine.id,
    engine: 'Pocket TTS v2 · Q8',
    voice: voice.label,
    language: voice.language,
    description: `Private, on-device ${voice.language} voice.`,
    sampleRate: 24_000,
    temperature: 0.3,
    sharedBytes: engine.storedBytes,
    profileBytes,
    storedBytes: engine.storedBytes + profileBytes,
    packages: Object.freeze([engine.package, profile]),
  })
}))

export const DEFAULT_SPEECH_MODEL_ID = builtInModels[0].id

export function speechEngines() { return engines }
export function speechEngine(engineId) { return engines.find((engine) => engine.id === engineId) || null }

function profileStorageError(message, cause, code = 'storage_corrupt') {
  const error = new Error(message, cause ? { cause } : undefined)
  error.code = code
  return error
}

function encodedPcmBytes(value) {
  if (typeof value !== 'string' || value.length % 4 !== 0
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
    return 0
  }
  const padding = value.endsWith('==') ? 2 : (value.endsWith('=') ? 1 : 0)
  return value.length / 4 * 3 - padding
}

function normalizeClonedProfile(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || !LANGUAGE_SPECS.some((language) => language.id === value.languageId)
    || (value.name !== undefined && typeof value.name !== 'string')) {
    return null
  }
  const pcmBytes = encodedPcmBytes(value.pcm16Base64)
  if (pcmBytes < MIN_CLONE_PCM_BYTES || pcmBytes > MAX_CLONE_PCM_BYTES || pcmBytes % 2 !== 0) {
    return null
  }
  const profile = {
    languageId: value.languageId,
    name: String(value.name || '').trim().slice(0, 40) || 'My voice',
    pcm16Base64: value.pcm16Base64,
  }
  return Object.freeze(profile)
}

function clonedProfileState(storage = globalThis.localStorage) {
  let raw
  try {
    raw = storage?.getItem?.(CLONED_PROFILES_KEY)
  } catch (error) {
    return {
      profiles: [],
      error: profileStorageError('The saved voice library is unavailable.', error, 'storage_unavailable'),
    }
  }
  if (raw == null || raw === '') return { profiles: [], error: null }
  let values
  try { values = JSON.parse(raw) } catch (error) {
    return { profiles: [], error: profileStorageError('The saved voice library is damaged.', error) }
  }
  if (!Array.isArray(values)) {
    return { profiles: [], error: profileStorageError('The saved voice library is damaged.') }
  }
  const profiles = []
  const languages = new Set()
  for (const value of values) {
    const profile = normalizeClonedProfile(value)
    if (!profile || languages.has(profile.languageId)) {
      return { profiles: [], error: profileStorageError('The saved voice library is damaged.') }
    }
    profiles.push(profile)
    languages.add(profile.languageId)
  }
  return { profiles, error: null }
}

function readClonedProfiles(storage = globalThis.localStorage) {
  return clonedProfileState(storage).profiles
}

export function clonedSpeechLibraryStatus(storage = globalThis.localStorage) {
  const error = clonedProfileState(storage).error
  if (!error) return 'ready'
  return error.code === 'storage_unavailable' ? 'unavailable' : 'damaged'
}

export function isClonedSpeechModelId(modelId) {
  return LANGUAGE_SPECS.some((language) => (
    modelId === `pocket-tts-clone-${language.id}`
  ))
}

export function resetClonedSpeechProfiles(storage = globalThis.localStorage) {
  if (!storage || typeof storage.removeItem !== 'function') {
    throw profileStorageError(
      'The saved voice library is unavailable.',
      undefined,
      'storage_unavailable',
    )
  }
  try {
    storage.removeItem(CLONED_PROFILES_KEY)
  } catch (error) {
    throw profileStorageError(
      'The saved voice library could not be reset.',
      error,
      'storage_unavailable',
    )
  }
}

function pcmFingerprint(value) {
  // This only invalidates an in-memory model cache; it is not an integrity
  // check. FNV-1a keeps the identity compact and deterministic for existing
  // profiles without rewriting their data.
  let hash = 0xcbf29ce484222325n
  for (let index = 0; index < value.length; index += 1) {
    hash ^= BigInt(value.charCodeAt(index))
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return `${value.length.toString(36)}-${hash.toString(16).padStart(16, '0')}`
}

function clonedModel(profile) {
  const language = LANGUAGE_SPECS.find((item) => item.id === profile.languageId)
  const engine = engines.find((item) => item.languages[0] === language.label)
  const profileBytes = encodedPcmBytes(profile.pcm16Base64)
  return Object.freeze({
    id: `pocket-tts-clone-${language.id}`,
    name: profile.name || 'My voice',
    engineId: engine.id,
    engine: 'Pocket TTS v2 · Q8',
    voice: profile.name || 'My voice',
    language: language.label,
    languageId: language.id,
    description: `Private cloned ${language.label} voice.`,
    sampleRate: 24_000,
    temperature: 0.3,
    sharedBytes: engine.storedBytes,
    profileBytes,
    storedBytes: engine.storedBytes,
    packages: Object.freeze([engine.package]),
    cloned: true,
    loadIdentity: `pocket-tts-clone-${language.id}:${pcmFingerprint(profile.pcm16Base64)}`,
    clonePcm16Base64: profile.pcm16Base64,
  })
}

export function speechModels(storage = globalThis.localStorage) {
  return [...builtInModels, ...readClonedProfiles(storage).map(clonedModel)]
}
export function speechModel(modelId, storage = globalThis.localStorage) {
  return speechModels(storage).find((model) => model.id === modelId) || null
}
export function speechModelPackages(model) { return model?.packages || [] }

function modelAssetBytes(model) {
  return Object.fromEntries(model.packages.flatMap((pkg) => (
    pkg.assets.map((item) => [item.id, item.bytes])
  )))
}

function bytesFromBase64(value) {
  const binary = globalThis.atob(value)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return bytes
}

function modelCloneSamples(model) {
  if (!model?.cloned) return null
  let bytes
  try { bytes = bytesFromBase64(model.clonePcm16Base64) } catch (error) {
    throw profileStorageError('The saved voice recording is damaged.', error)
  }
  if (bytes.byteLength < MIN_CLONE_PCM_BYTES
    || bytes.byteLength > MAX_CLONE_PCM_BYTES
    || bytes.byteLength % 2 !== 0) {
    throw profileStorageError('The saved voice recording is damaged.')
  }
  const pcm16 = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2))
  const samples = new Float32Array(pcm16.length)
  for (let index = 0; index < pcm16.length; index += 1) samples[index] = pcm16[index] / 32_768
  return samples
}

export function speechModelLoadSnapshot(modelId, storage = globalThis.localStorage) {
  const model = speechModel(modelId, storage)
  if (!model) return null
  return Object.freeze({
    modelId: model.id,
    identity: model.loadIdentity || model.id,
    assetBytes: modelAssetBytes(model),
    temperature: model.temperature || 0.3,
    clonedVoiceSamples: modelCloneSamples(model),
    packages: model.packages,
    storedBytes: model.storedBytes,
  })
}

// The bare engine for one language (tokenizer + model, no built-in voice). A
// consumer that supplies its own voice recording — a server-stored clone owned
// by the Voice app — streams this and passes the recording straight to the
// worker, so cloned voices no longer depend on the shell's local storage.
export function speechEngineLoadSnapshot(engineId) {
  const engine = speechEngine(engineId)
  if (!engine) return null
  const assetBytes = Object.fromEntries(engine.package.assets.map((item) => [item.id, item.bytes]))
  return Object.freeze({
    modelId: engine.id,
    identity: engine.id,
    assetBytes,
    temperature: 0.3,
    clonedVoiceSamples: null,
    packages: Object.freeze([engine.package]),
    storedBytes: engine.storedBytes,
  })
}

export function saveClonedSpeechProfile(profile, storage = globalThis.localStorage) {
  const current = clonedProfileState(storage)
  if (current.error) throw current.error
  const saved = normalizeClonedProfile(profile)
  if (!saved) throw new TypeError('The cloned voice profile is invalid.')
  const next = [...current.profiles.filter((item) => item.languageId !== saved.languageId), saved]
  storage?.setItem?.(CLONED_PROFILES_KEY, JSON.stringify(next))
  return clonedModel(saved)
}

export function removeClonedSpeechProfile(model, storage = globalThis.localStorage) {
  if (!model?.cloned) return false
  const current = clonedProfileState(storage)
  if (current.error) throw current.error
  const remaining = current.profiles.filter((item) => item.languageId !== model.languageId)
  storage?.setItem?.(CLONED_PROFILES_KEY, JSON.stringify(remaining))
  return true
}

export function publicSpeechModel(model) {
  return model ? {
    id: model.id, name: model.name, engineId: model.engineId, engine: model.engine,
    voice: model.voice, language: model.language, description: model.description,
    sampleRate: model.sampleRate, sharedBytes: model.sharedBytes,
    profileBytes: model.profileBytes, storedBytes: model.storedBytes, cloned: Boolean(model.cloned),
  } : null
}

export function publicSpeechEngine(engine) {
  return engine ? {
    id: engine.id, name: engine.name, delivery: engine.delivery,
    languages: [...engine.languages], storedBytes: engine.storedBytes,
  } : null
}
