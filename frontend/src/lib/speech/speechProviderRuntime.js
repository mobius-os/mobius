import { browserSpeechEngine, releaseBrowserSpeechEngine } from './pocketTtsEngine.js'
import {
  speechModelCatalog,
} from './speechModelStore.js'
import { DEFAULT_SPEECH_MODEL_ID } from './speechModels.js'

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

function cleanText(input, maxTextChars) {
  if (typeof input?.text !== 'string') {
    throw speechError('invalid_request', 'Speech text is required.', 'TypeError')
  }
  const text = input.text.replace(/\s+/g, ' ').trim()
  if (!text) throw speechError('invalid_request', 'Speech text cannot be empty.', 'TypeError')
  if (text.length > maxTextChars) {
    throw speechError(
      'invalid_request',
      `Speech text cannot exceed ${maxTextChars.toLocaleString()} characters.`,
      'TypeError',
    )
  }
  return text
}

async function resolvedModelId(requested, options = {}) {
  const catalog = await speechModelCatalog(options)
  const modelId = requested || catalog.activeModelId || DEFAULT_SPEECH_MODEL_ID
  const model = catalog.models.find((candidate) => candidate.id === modelId)
  if (!model || model.state !== 'ready') {
    throw speechError(
      'not_installed',
      'Open Voice and download the selected speech model on this device first.',
      'NotFoundError',
    )
  }
  return model
}

export function synthesizeSpeech({
  text,
  modelId,
  signal,
  onAudio,
  onLoading,
  maxTextChars = 50_000,
} = {}) {
  const normalizedText = cleanText({ text }, maxTextChars)
  const controller = new AbortController()
  const forwardAbort = () => controller.abort(signal?.reason || abortError())
  if (signal?.aborted) forwardAbort()
  else signal?.addEventListener?.('abort', forwardAbort, { once: true })

  if (activeSynthesis && !activeSynthesis.signal.aborted) {
    activeSynthesis.abort(abortError())
  }
  activeSynthesis = controller

  const run = async () => {
    if (controller.signal.aborted) throw controller.signal.reason || abortError()
    const model = await resolvedModelId(modelId)
    const engine = browserSpeechEngine()
    await engine.load({
      modelId: model.id,
      signal: controller.signal,
      onProgress: onLoading,
    })
    if (controller.signal.aborted) throw controller.signal.reason || abortError()
    onLoading?.({ stage: 'generating', percent: 100 })
    await engine.generate(normalizedText, {
      signal: controller.signal,
      onChunk: onAudio,
    })
    return { modelId: model.id, sampleRate: model.sampleRate }
  }

  const result = synthesisTail.catch(() => {}).then(run)
  synthesisTail = result.finally(() => {
    signal?.removeEventListener?.('abort', forwardAbort)
    if (activeSynthesis === controller) activeSynthesis = null
  })
  synthesisTail.catch(() => {})
  return {
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

export function openSpeechCapability({ input, declaration, channel }) {
  const operation = input?.operation || 'synthesize'
  if (operation === 'catalog') {
    Promise.resolve().then(async () => {
      const catalog = await speechModelCatalog()
      channel.ready(catalog)
      channel.result(catalog)
    }).catch(channel.error)
    return {}
  }
  if (operation !== 'synthesize') {
    throw speechError('invalid_request', 'Unknown speech operation.', 'TypeError')
  }
  const maxTextChars = Number(declaration?.limits?.max_text_chars) || 50_000
  const synthesis = synthesizeSpeech({
    text: input.text,
    modelId: input.modelId,
    maxTextChars,
    onLoading(value) { channel.event('loading', value) },
    onAudio(samples) {
      channel.event('audio', { samples }, samples?.buffer ? [samples.buffer] : [])
    },
  })
  channel.ready({ state: 'starting' })
  synthesis.result.then(channel.result).catch(channel.error)
  return {
    control(action) {
      if (action === 'cancel') synthesis.cancel()
    },
  }
}
