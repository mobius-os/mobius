import { createDeviceAssetCacheProvider } from '../deviceAssetCache.js'
import {
  DEFAULT_SPEECH_MODEL_ID,
  publicSpeechModel,
  SPEECH_MODEL_PARTITION,
  SPEECH_MODEL_STORAGE_LIMITS,
  speechModel,
  speechModels,
} from './speechModels.js'

const ACTIVE_MODEL_KEY = 'mobius:speech:active-model:v1'

function speechError(code, message, name = 'CapabilityError') {
  const error = new Error(message)
  error.code = code
  error.name = name
  return error
}

function requireModel(modelId) {
  const model = speechModel(modelId)
  if (!model) throw speechError('invalid_request', 'Unknown speech model.', 'TypeError')
  return model
}

function invokeAssetOperation({
  appId,
  declaration,
  model,
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
      input: { operation, package: model.package },
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

export async function speechModelStatus(modelId, options = {}) {
  const model = requireModel(modelId)
  const declaration = { limits: SPEECH_MODEL_STORAGE_LIMITS }
  const state = await invokeAssetOperation({
    ...options, declaration, model, operation: 'status',
  })
  return { ...publicSpeechModel(model), ...state }
}

export async function speechModelCatalog(options = {}) {
  const storage = activeStorage(options.storage)
  const values = []
  for (const model of speechModels()) {
    const state = await speechModelStatus(model.id, options)
    values.push(state)
  }
  const requested = storage.get()
  const active = values.find((model) => model.id === requested && model.state === 'ready')
    || values.find((model) => model.state === 'ready')
    || null
  return {
    activeModelId: active?.id || requested,
    models: values,
  }
}

export async function installSpeechModel(modelId, {
  appId,
  declaration,
  storage,
  ...options
} = {}) {
  const model = requireModel(modelId)
  const value = await invokeAssetOperation({
    ...options, appId, declaration, model, operation: 'install',
  })
  if (value.state === 'ready') activeStorage(storage).set(model.id)
  return { ...publicSpeechModel(model), ...value }
}

export async function selectSpeechModel(modelId, options = {}) {
  const status = await speechModelStatus(modelId, options)
  if (status.state !== 'ready') {
    throw speechError(
      'not_installed',
      'Download this speech model on this device before selecting it.',
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
  const model = requireModel(modelId)
  const value = await invokeAssetOperation({
    ...options, appId, declaration, model, operation: 'remove',
  })
  activeStorage(storage).clear(modelId)
  return { ...publicSpeechModel(model), ...value }
}

export async function streamSpeechModel(modelId, options = {}) {
  const model = requireModel(modelId)
  let transferred = 0
  await invokeAssetOperation({
    ...options,
    declaration: { limits: SPEECH_MODEL_STORAGE_LIMITS },
    model,
    operation: 'read',
    onChunk: async (value) => {
      transferred += value?.bytes?.byteLength || 0
      options.onProgress?.(Math.max(1, Math.min(
        96,
        Math.round(transferred / model.storedBytes * 96),
      )))
      await options.onChunk?.(value)
    },
  })
  return model
}
