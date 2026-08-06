import {
  installSpeechEngine,
  installSpeechModel,
  installSpeechProfile,
  removeSpeechEngine,
  removeSpeechModel,
  resetSpeechClones,
  saveSpeechClone,
  selectSpeechModel,
  speechModelCatalog,
  streamSpeechModel,
} from './speechModelStore.js'
import {
  DEFAULT_SPEECH_ENGINE_ID,
  DEFAULT_SPEECH_MODEL_ID,
  speechEngineLoadSnapshot,
  speechModelLoadSnapshot,
} from './speechModels.js'

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
  storage = globalThis.localStorage,
}) {
  const operation = input?.operation || 'catalog'
  const controller = new AbortController()
  let allowStart = () => {}
  const started = new Promise((resolve) => { allowStart = resolve })
  const modelId = input?.modelId || DEFAULT_SPEECH_MODEL_ID
  const engineId = input?.engineId || DEFAULT_SPEECH_ENGINE_ID
  const progress = (value) => channel.event('progress', value)
  const options = {
    appId, declaration, signal: controller.signal, onProgress: progress, storage,
  }
  const task = Promise.resolve().then(async () => {
    if (operation === 'catalog') return speechModelCatalog(options)
    if (operation === 'install-engine') return installSpeechEngine(engineId, options)
    if (operation === 'install-profile') return installSpeechProfile(modelId, options)
    if (operation === 'install') return installSpeechModel(modelId, options)
    if (operation === 'select') return selectSpeechModel(modelId, options)
    // Stream the bytes of a model this device already holds, so a consumer can
    // run the engine in its own frame. A document's Content-Security-Policy
    // governs the workers it spawns, and the shell's is not always the policy
    // that permits WebAssembly, so synthesis cannot only ever live here.
    if (operation === 'read') {
      // `engineId` streams the bare language engine so a consumer can supply
      // its own voice recording (a server-stored clone). `modelId` streams a
      // built-in voice or a legacy local clone.
      const snapshot = input?.engineId
        ? speechEngineLoadSnapshot(input.engineId)
        : speechModelLoadSnapshot(modelId, storage)
      if (!snapshot) {
        throw speechError(
          'not_installed',
          'Download this voice on this device before reading it.',
          'NotFoundError',
        )
      }
      channel.event('manifest', {
        assetBytes: snapshot.assetBytes,
        temperature: snapshot.temperature,
        clonedVoiceSamples: snapshot.clonedVoiceSamples || null,
      })
      // The consumer needs the manifest to size its buffer, and the bytes must
      // not start arriving until that buffer exists. Waiting for its `start`
      // both sequences the two and keeps the model from piling up in the
      // consumer's memory while its engine is still warming up.
      await started
      await streamSpeechModel(snapshot, {
        signal: controller.signal,
        onProgress: (percent) => progress({ percent }),
        // Hand each chunk straight to the consumer; buffering the whole model
        // here would duplicate 148 MB before it ever reaches the engine.
        onChunk: (value) => channel.event('chunk', value, [value.bytes]),
      })
      return { modelId: snapshot.modelId, assetBytes: snapshot.assetBytes }
    }
    if (operation === 'save-clone') return saveSpeechClone(input, options)
    if (operation === 'reset-clones') {
      if (input?.confirm !== true) {
        throw speechError(
          'confirmation_required',
          'Confirm before resetting the cloned voice library.',
          'TypeError',
        )
      }
      return resetSpeechClones(options)
    }
    if (operation === 'remove') {
      const [{ disposeSpeechEngine }, value] = await Promise.all([
        import('./speechProviderRuntime.js'),
        removeSpeechModel(modelId, options),
      ])
      disposeSpeechEngine()
      return value
    }
    if (operation === 'remove-engine') {
      const [{ disposeSpeechEngine }, value] = await Promise.all([
        import('./speechProviderRuntime.js'),
        removeSpeechEngine(engineId, options),
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
      if (action === 'start') allowStart()
      if (action === 'cancel' && !controller.signal.aborted) {
        allowStart()
        controller.abort(abortError())
      }
    },
  }
}
