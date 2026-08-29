import { createDeviceAssetCacheProvider } from '../deviceAssetCache.js'
import {
  clonedSpeechLibraryStatus,
  DEFAULT_SPEECH_ENGINE_ID,
  DEFAULT_SPEECH_MODEL_ID,
  hasUsableCloneSignal,
  isClonedSpeechModelId,
  publicSpeechEngine,
  publicSpeechModel,
  resetClonedSpeechProfiles,
  SPEECH_MODEL_PARTITION,
  SPEECH_MODEL_STORAGE_LIMITS,
  speechEngine,
  speechEngines,
  speechModel,
  speechModelPackages,
  speechModels,
  removeClonedSpeechProfile,
  saveClonedSpeechProfile,
} from './speechModels.js'
import { SPEECH_PITCH_WORKLET_URL } from './speechPitchAsset.js'

const ACTIVE_MODEL_KEY = 'mobius:speech:active-model:v1'
const CLONE_SECONDS = 8
const MAX_CLONE_SAMPLE_RATE = 384_000

function speechError(code, message, name = 'CapabilityError') {
  const error = new Error(message)
  error.code = code
  error.name = name
  return error
}

function requireModel(modelId, storage) {
  const model = speechModel(modelId, storage)
  if (!model) throw speechError('invalid_request', 'Unknown speech model.', 'TypeError')
  return model
}

function requireEngine(engineId) {
  const engine = speechEngine(engineId)
  if (!engine) throw speechError('invalid_request', 'Unknown speech engine.', 'TypeError')
  return engine
}

function invokeAssetOperation({
  appId,
  declaration,
  pkg,
  operation,
  signal,
  onChunk,
  onProgress,
  dependencies,
}) {
  const provider = createDeviceAssetCacheProvider({
    appId,
    partitionId: SPEECH_MODEL_PARTITION,
    ...dependencies,
  })
  let control
  let consumerError = null
  let removeAbort = () => {}
  const result = new Promise((resolve, reject) => {
    control = provider.open({
      input: { operation, package: pkg },
      declaration,
      channel: {
        ready() {},
        event(name, value) {
          if (name === 'progress') onProgress?.(value)
          if (name !== 'chunk') return
          Promise.resolve(onChunk?.(value)).then(
            () => control.control('next'),
            (error) => {
              consumerError = error
              control.control('cancel')
            },
          )
        },
        result(value) { consumerError ? reject(consumerError) : resolve(value) },
        error: reject,
      },
    })
    if (signal) {
      const cancel = () => control.control('cancel')
      if (signal.aborted) cancel()
      else signal.addEventListener('abort', cancel, { once: true })
      removeAbort = () => signal.removeEventListener('abort', cancel)
    }
  })
  return result.finally(removeAbort)
}

function activeStorage(storage = globalThis.localStorage) {
  return {
    get() {
      try { return storage?.getItem?.(ACTIVE_MODEL_KEY) || DEFAULT_SPEECH_MODEL_ID } catch {
        return DEFAULT_SPEECH_MODEL_ID
      }
    },
    set(value) {
      try { storage?.setItem?.(ACTIVE_MODEL_KEY, value) } catch {}
    },
    clear(value) {
      try {
        if (storage?.getItem?.(ACTIVE_MODEL_KEY) === value) storage.removeItem(ACTIVE_MODEL_KEY)
      } catch {}
    },
  }
}

function packageBytes(pkg) {
  return pkg.assets.reduce((total, asset) => total + asset.bytes, 0)
}

function combinedState(states) {
  const cachedBytes = states.reduce((total, value) => total + value.cachedBytes, 0)
  const totalBytes = states.reduce((total, value) => total + value.totalBytes, 0)
  const cachedChunks = states.reduce((total, value) => total + value.cachedChunks, 0)
  const totalChunks = states.reduce((total, value) => total + value.totalChunks, 0)
  return {
    state: states.every((value) => value.state === 'ready')
      ? 'ready'
      : (cachedBytes > 0 ? 'partial' : 'missing'),
    cachedBytes,
    totalBytes,
    cachedChunks,
    totalChunks,
    persistence: states.every((value) => value.persistence === 'persistent')
      ? 'persistent'
      : 'best-effort',
  }
}

function statusPackage(pkg, options = {}) {
  return invokeAssetOperation({
    ...options,
    declaration: { limits: SPEECH_MODEL_STORAGE_LIMITS },
    pkg,
    operation: 'status',
  })
}

export async function speechModelStatus(modelId, options = {}) {
  const model = requireModel(modelId, options.storage)
  const states = []
  for (const pkg of speechModelPackages(model)) states.push(await statusPackage(pkg, options))
  return { ...publicSpeechModel(model), ...combinedState(states) }
}

export async function speechEngineStatus(engineId = DEFAULT_SPEECH_ENGINE_ID, options = {}) {
  const engine = requireEngine(engineId)
  const state = await statusPackage(engine.package, options)
  return { ...publicSpeechEngine(engine), ...state }
}

export async function speechModelCatalog(options = {}) {
  const storage = activeStorage(options.storage)
  const available = speechModels(options.storage)
  const engineStates = new Map()
  for (const engine of speechEngines()) {
    engineStates.set(engine.id, await statusPackage(engine.package, options))
  }
  const values = []
  for (const model of available) {
    if (model.cloned) {
      const engineState = engineStates.get(model.engineId)
      values.push({
        ...publicSpeechModel(model),
        ...combinedState([engineState]),
        profileState: 'ready',
        profileCachedBytes: model.profileBytes,
        profileCachedChunks: 1,
        profileTotalChunks: 1,
        profilePersistence: 'persistent',
      })
      continue
    }
    const voiceState = await statusPackage(speechModelPackages(model)[1], options)
    values.push({
      ...publicSpeechModel(model),
      ...combinedState([engineStates.get(model.engineId), voiceState]),
      profileState: voiceState.state,
      profileCachedBytes: voiceState.cachedBytes,
      profileCachedChunks: voiceState.cachedChunks,
      profileTotalChunks: voiceState.totalChunks,
      profilePersistence: voiceState.persistence,
    })
  }
  const requested = storage.get()
  const active = values.find((model) => model.id === requested && model.state === 'ready')
    || values.find((model) => model.state === 'ready')
    || null
  return {
    activeModelId: active?.id || requested,
    cloneLibrary: { status: clonedSpeechLibraryStatus(options.storage) },
    engines: speechEngines().map((engine) => ({
      ...publicSpeechEngine(engine),
      ...engineStates.get(engine.id),
    })),
    models: values,
  }
}

function playbackModel(model) {
  return model ? {
    id: model.id,
    name: model.name,
    language: model.language,
    sampleRate: model.sampleRate,
  } : null
}

export async function speechPlaybackCatalog(options = {}) {
  const catalog = await speechModelCatalog(options)
  const active = catalog.models.find((model) => (
    model.id === catalog.activeModelId && model.state === 'ready'
  )) || null
  return {
    activeModel: playbackModel(active),
    playback: {
      pitchPreserving: true,
      workletUrl: SPEECH_PITCH_WORKLET_URL,
    },
  }
}

export function resetSpeechClones({ storage = globalThis.localStorage } = {}) {
  let activeModelId
  try {
    activeModelId = storage?.getItem?.(ACTIVE_MODEL_KEY) || null
  } catch (error) {
    throw speechError(
      'storage_unavailable',
      'The saved voice library is unavailable.',
      error?.name || 'NotAllowedError',
    )
  }
  if (isClonedSpeechModelId(activeModelId)) {
    try {
      storage.removeItem(ACTIVE_MODEL_KEY)
    } catch (error) {
      throw speechError(
        'storage_unavailable',
        'The active cloned voice could not be cleared.',
        error?.name || 'NotAllowedError',
      )
    }
  }
  resetClonedSpeechProfiles(storage)
  return { cloneLibrary: { status: 'ready' } }
}

export async function installSpeechEngine(engineId = DEFAULT_SPEECH_ENGINE_ID, {
  appId,
  declaration,
  onProgress,
  ...options
} = {}) {
  const engine = requireEngine(engineId)
  await invokeAssetOperation({
    ...options,
    appId,
    declaration,
    pkg: engine.package,
    operation: 'install',
    onProgress,
  })
  onProgress?.({ downloadedBytes: engine.storedBytes, totalBytes: engine.storedBytes })
  return speechEngineStatus(engine.id, options)
}

export async function installSpeechProfile(modelId, {
  appId,
  declaration,
  storage,
  onProgress,
  ...options
} = {}) {
  const model = requireModel(modelId, storage)
  if (model.cloned) {
    const status = await speechModelStatus(model.id, { ...options, storage })
    if (status.state === 'ready') activeStorage(storage).set(model.id)
    return status
  }
  const [, profile] = speechModelPackages(model)
  await invokeAssetOperation({
    ...options,
    appId,
    declaration,
    pkg: profile,
    operation: 'install',
    onProgress,
  })
  onProgress?.({ downloadedBytes: model.profileBytes, totalBytes: model.profileBytes })
  const status = await speechModelStatus(model.id, { ...options, storage })
  if (status.state === 'ready') activeStorage(storage).set(model.id)
  return status
}

export async function installSpeechModel(modelId, {
  appId,
  declaration,
  storage,
  onProgress,
  ...options
} = {}) {
  const model = requireModel(modelId, storage)
  let completedBytes = 0
  for (const pkg of speechModelPackages(model)) {
    await invokeAssetOperation({
      ...options,
      appId,
      declaration,
      pkg,
      operation: 'install',
      onProgress(value) {
        onProgress?.({
          ...value,
          downloadedBytes: completedBytes + value.downloadedBytes,
          totalBytes: model.storedBytes,
        })
      },
    })
    completedBytes += packageBytes(pkg)
  }
  onProgress?.({ downloadedBytes: model.storedBytes, totalBytes: model.storedBytes })
  activeStorage(storage).set(model.id)
  return speechModelStatus(model.id, { ...options, storage })
}

export async function selectSpeechModel(modelId, options = {}) {
  const status = await speechModelStatus(modelId, options)
  if (status.state !== 'ready') {
    throw speechError(
      'not_installed',
      'Download this voice on this device before selecting it.',
      'NotFoundError',
    )
  }
  activeStorage(options.storage).set(modelId)
  return { activeModelId: modelId }
}

export async function removeSpeechModel(modelId, {
  appId,
  declaration,
  storage,
  ...options
} = {}) {
  const model = requireModel(modelId, storage)
  if (model.cloned) {
    removeClonedSpeechProfile(model, storage)
    activeStorage(storage).clear(modelId)
    return { ...publicSpeechModel(model), state: 'removed' }
  }
  const [, voicePackage] = speechModelPackages(model)
  const value = await invokeAssetOperation({
    ...options,
    appId,
    declaration,
    pkg: voicePackage,
    operation: 'remove',
  })
  activeStorage(storage).clear(modelId)
  return { ...publicSpeechModel(model), ...value }
}

export async function removeSpeechEngine(engineId, {
  appId,
  declaration,
  storage,
  ...options
} = {}) {
  const engine = requireEngine(engineId)
  const value = await invokeAssetOperation({
    ...options,
    appId,
    declaration,
    pkg: engine.package,
    operation: 'remove',
  })
  const active = activeStorage(storage)
  for (const model of speechModels(storage)) {
    if (model.engineId === engine.id) active.clear(model.id)
  }
  return { ...publicSpeechEngine(engine), ...value }
}

function resampleMono(samples, sourceRate, targetRate = 24_000) {
  if (sourceRate === targetRate) return new Float32Array(samples)
  const length = Math.max(1, Math.round(samples.length * targetRate / sourceRate))
  const result = new Float32Array(length)
  const ratio = sourceRate / targetRate
  for (let index = 0; index < length; index += 1) {
    const position = index * ratio
    const left = Math.min(samples.length - 1, Math.floor(position))
    const right = Math.min(samples.length - 1, left + 1)
    const mix = position - left
    result[index] = samples[left] * (1 - mix) + samples[right] * mix
  }
  return result
}

function base64Pcm16(samples) {
  const pcm = new Int16Array(samples.length)
  for (let index = 0; index < samples.length; index += 1) {
    pcm[index] = Math.max(-32_768, Math.min(32_767, Math.round(samples[index] * 32_767)))
  }
  const bytes = new Uint8Array(pcm.buffer)
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 32_768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768))
  }
  return globalThis.btoa(binary)
}

export function saveSpeechClone({ language, name, samples, sampleRate }, { storage } = {}) {
  if (!(samples instanceof Float32Array)
    || !Number.isSafeInteger(sampleRate)
    || sampleRate < 8_000
    || sampleRate > MAX_CLONE_SAMPLE_RATE) {
    throw speechError('invalid_request', 'The voice recording is invalid.', 'TypeError')
  }
  const languageId = String(language || '').trim().toLowerCase()
  const engine = speechEngines().find((item) => item.languages[0].toLowerCase() === languageId)
  if (!engine) throw speechError('invalid_request', 'Choose a supported language.', 'TypeError')
  // Bound work at the untrusted capability boundary, before output allocation.
  const boundedSamples = samples.subarray(0, sampleRate * CLONE_SECONDS)
  const resampled = resampleMono(boundedSamples, sampleRate)
  if (resampled.length < 24_000 * 3) {
    throw speechError('invalid_request', 'Record at least three seconds of clear speech.', 'TypeError')
  }
  if (!hasUsableCloneSignal(resampled)) {
    throw speechError(
      'invalid_request',
      'We could not hear clear speech in that recording. Check your microphone and record again.',
      'TypeError',
    )
  }
  const targetStorage = storage || globalThis.localStorage
  let model
  try {
    model = saveClonedSpeechProfile({
      languageId,
      name: String(name || '').trim().slice(0, 40) || 'My voice',
      pcm16Base64: base64Pcm16(resampled),
    }, targetStorage)
  } catch (error) {
    if (error?.name === 'QuotaExceededError') {
      throw speechError('storage_full', 'This device could not save another cloned voice.', error.name)
    }
    throw speechError(
      error?.code === 'storage_corrupt' ? 'storage_corrupt' : 'storage_unavailable',
      error?.code === 'storage_corrupt'
        ? error.message
        : 'This device could not safely save the cloned voice.',
      error?.name || 'NotAllowedError',
    )
  }
  activeStorage(targetStorage).set(model.id)
  return publicSpeechModel(model)
}

export async function streamSpeechModel(snapshot, options = {}) {
  let transferred = 0
  for (const pkg of snapshot.packages) {
    await invokeAssetOperation({
      ...options,
      declaration: { limits: SPEECH_MODEL_STORAGE_LIMITS },
      pkg,
      operation: 'read',
      onChunk: async (value) => {
        transferred += value?.bytes?.byteLength || 0
        options.onProgress?.(Math.max(1, Math.min(
          96,
          Math.round(transferred / snapshot.storedBytes * 96),
        )))
        await options.onChunk?.(value)
      },
    })
  }
  return snapshot
}
