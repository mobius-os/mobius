import { browserSpeechEngine, releaseBrowserSpeechEngine } from './pocketTtsEngine.js'
import {
  speechModelCatalog, speechPlaybackCatalog, streamSpeechModel,
} from './speechModelStore.js'
import {
  DEFAULT_SPEECH_MODEL_ID, speechEngineLoadSnapshot, speechModelLoadSnapshot,
} from './speechModels.js'
import { normalizeSpeechInput } from './speechDocument.js'

let activeSynthesis = null
let synthesisTail = Promise.resolve()

function speechError(code, message, name = 'CapabilityError') {
  const error = new Error(message)
  error.code = code
  error.name = name
  return error
}

function abortError() {
  return speechError('aborted', 'Speech stopped.', 'AbortError')
}

async function resolvedModel(requested, options = {}) {
  const catalog = await speechModelCatalog(options)
  const modelId = requested || catalog.activeModelId || DEFAULT_SPEECH_MODEL_ID
  const model = catalog.models.find((candidate) => candidate.id === modelId)
  if (!model || model.state !== 'ready') {
    throw speechError(
      'not_installed',
      'Open Voice and download the selected voice on this device first.',
      'NotFoundError',
    )
  }
  return model
}

export function synthesizeSpeech({
  text,
  document,
  modelId,
  signal,
  onAudio,
  onLoading,
  onBoundary,
  maxTextChars = 50_000,
} = {}) {
  const normalizedDocument = normalizeSpeechInput({ text, document }, maxTextChars)
  const controller = new AbortController()
  const forwardAbort = () => controller.abort(signal?.reason || abortError())
  if (signal?.aborted) forwardAbort()
  else signal?.addEventListener?.('abort', forwardAbort, { once: true })

  if (activeSynthesis && !activeSynthesis.signal.aborted) {
    activeSynthesis.abort(abortError())
  }
  activeSynthesis = controller

  let resolveReady
  let rejectReady
  let readySettled = false
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve
    rejectReady = reject
  })
  // Capability consumers may only need streamed events and the final result.
  // Keep a rejected readiness handshake from becoming unrelated console noise.
  ready.catch(() => {})

  const run = async () => {
    try {
      if (controller.signal.aborted) throw controller.signal.reason || abortError()
      const model = await resolvedModel(modelId, { signal: controller.signal })
      const engine = browserSpeechEngine()
      await engine.load({
        modelId: model.id,
        signal: controller.signal,
        onProgress: onLoading,
      })
      if (controller.signal.aborted) throw controller.signal.reason || abortError()
      const metadata = {
        modelId: model.id,
        sampleRate: model.sampleRate,
        segmentCount: normalizedDocument.segments.length,
      }
      readySettled = true
      resolveReady(metadata)
      // Readiness owns the model metadata needed to interpret audio. Let its
      // consumers observe that contract before the first generation can emit.
      await Promise.resolve()
      for (let index = 0; index < normalizedDocument.segments.length; index += 1) {
        if (controller.signal.aborted) throw controller.signal.reason || abortError()
        const segment = normalizedDocument.segments[index]
        onLoading?.({
          stage: 'generating',
          percent: Math.round(index / normalizedDocument.segments.length * 100),
          segmentIndex: index,
          segmentCount: normalizedDocument.segments.length,
        })
        await engine.generate(segment.text, {
          signal: controller.signal,
          onChunk: onAudio,
        })
        onBoundary?.({
          segmentIndex: index,
          kind: segment.kind,
          pauseAfterMs: segment.pauseAfterMs,
        })
      }
      onLoading?.({ stage: 'generating', percent: 100 })
      return metadata
    } catch (error) {
      if (!readySettled) {
        readySettled = true
        rejectReady(error)
      }
      throw error
    }
  }

  const result = synthesisTail.catch(() => {}).then(run)
  synthesisTail = result.finally(() => {
    signal?.removeEventListener?.('abort', forwardAbort)
    if (activeSynthesis === controller) activeSynthesis = null
  })
  synthesisTail.catch(() => {})
  return {
    ready,
    result,
    cancel() {
      if (!controller.signal.aborted) controller.abort(abortError())
    },
  }
}

export function disposeSpeechEngine() {
  activeSynthesis?.abort(abortError())
  releaseBrowserSpeechEngine()
}

export function openSpeechCapability({
  input,
  declaration,
  channel,
  storage = globalThis.localStorage,
  dependencies,
}) {
  const operation = input?.operation || 'synthesize'
  if (operation === 'catalog') {
    Promise.resolve().then(() => speechPlaybackCatalog({ storage, dependencies })).then((catalog) => {
      channel.ready(catalog)
      channel.result(catalog)
    }).catch(channel.error)
    return {}
  }
  // Supply model bytes to a consumer that runs the engine in its own frame
  // (the default, via the frame runtime). `engineId` streams the bare language
  // engine so the caller can bring its own recording (a server-stored clone);
  // `modelId` streams a built-in voice. This needs no extra permission because
  // it rides the same `media.speech` grant the consumer already holds.
  if (operation === 'model-stream') {
    const controller = new AbortController()
    let allowStart = () => {}
    const started = new Promise((resolve) => { allowStart = resolve })
    Promise.resolve().then(async () => {
      const snapshot = input?.engineId
        ? speechEngineLoadSnapshot(input.engineId)
        : speechModelLoadSnapshot(input?.modelId, storage)
      if (!snapshot) {
        throw speechError(
          'not_installed',
          'Open Voice and download this voice on this device first.',
          'NotFoundError',
        )
      }
      channel.event('manifest', {
        assetBytes: snapshot.assetBytes,
        temperature: snapshot.temperature,
        clonedVoiceSamples: snapshot.clonedVoiceSamples || null,
      })
      // The consumer sizes its buffer from the manifest, so bytes must not flow
      // until it says the buffer exists — otherwise the model is held twice.
      await started
      await streamSpeechModel(snapshot, {
        signal: controller.signal,
        onProgress: (percent) => channel.event('progress', { percent }),
        onChunk: (value) => channel.event('chunk', value, [value.bytes]),
      })
      return { modelId: snapshot.modelId }
    }).then(channel.result).catch(channel.error)
    channel.ready({ state: 'starting' })
    return {
      control(action) {
        if (action === 'start') allowStart()
        if (action === 'cancel') {
          allowStart()
          if (!controller.signal.aborted) controller.abort(abortError())
        }
      },
    }
  }
  if (operation !== 'synthesize') {
    throw speechError('invalid_request', 'Unknown speech operation.', 'TypeError')
  }
  const maxTextChars = Number(declaration?.limits?.max_text_chars) || 50_000
  const synthesis = synthesizeSpeech({
    text: input.text,
    document: input.document,
    modelId: input.modelId,
    maxTextChars,
    onLoading(value) { channel.event('loading', value) },
    onAudio(samples) {
      channel.event('audio', { samples }, samples?.buffer ? [samples.buffer] : [])
    },
    onBoundary(value) { channel.event('boundary', value) },
  })
  channel.ready({ state: 'starting' })
  synthesis.result.then(channel.result).catch(channel.error)
  return {
    control(action) {
      if (action === 'cancel') synthesis.cancel()
    },
  }
}
