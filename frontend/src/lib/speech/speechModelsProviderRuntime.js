import {
  installSpeechModel,
  removeSpeechModel,
  selectSpeechModel,
  speechModelCatalog,
} from './speechModelStore.js'
import { DEFAULT_SPEECH_MODEL_ID } from './speechModels.js'

function speechError(code, message, name = 'CapabilityError') {
  const error = new Error(message)
  error.code = code
  error.name = name
  return error
}

function abortError() {
  return speechError('aborted', 'Speech-model operation stopped.', 'AbortError')
}

export function openSpeechModelsCapability({
  appId,
  input,
  declaration,
  channel,
}) {
  const operation = input?.operation || 'catalog'
  const controller = new AbortController()
  const modelId = input?.modelId || DEFAULT_SPEECH_MODEL_ID
  const progress = (value) => channel.event('progress', value)
  const options = { appId, declaration, signal: controller.signal, onProgress: progress }
  const task = Promise.resolve().then(async () => {
    if (operation === 'catalog') return speechModelCatalog()
    if (operation === 'install') return installSpeechModel(modelId, options)
    if (operation === 'select') return selectSpeechModel(modelId, options)
    if (operation === 'remove') {
      const [{ disposeSpeechEngine }, value] = await Promise.all([
        import('./speechProviderRuntime.js'),
        removeSpeechModel(modelId, options),
      ])
      disposeSpeechEngine()
      return value
    }
    throw speechError('invalid_request', 'Unknown speech-model operation.', 'TypeError')
  })
  channel.ready({ state: 'starting', operation })
  task.then(channel.result).catch(channel.error)
  return {
    control(action) {
      if (action === 'cancel' && !controller.signal.aborted) {
        controller.abort(abortError())
      }
    },
  }
}
