import { SpeechGenerationOwnership } from './speechGenerationOwnership.js'
import initXnRuntime, {
  allocate_model_weights as allocateModelWeights,
  free_allocated_model_weights as freeAllocatedModelWeights,
  model_from_allocated_weights as modelFromAllocatedWeights,
} from './pocketTtsXnRuntime.js'
import { completePostEosFrames } from './pocketTtsGenerationPolicy.js'

// XN's Q8 Pocket TTS runtime, isolated in a dedicated worker. The XN runtime is
// bundled into this script at build time and its Wasm binary is fetched from
// public/speech/. Model assets arrive from Möbius's checksum-verified device
// cache; the worker never fetches those.

const REQUIRED_ASSETS = Object.freeze(['tokenizer', 'model', 'voice'])
const MAX_ASSET_BYTES = 256 * 1024 * 1024
// Resolved against this script's own URL when it is served normally. A consumer
// running the engine inside an opaque-origin app frame must build the worker
// from a Blob (a sandboxed document cannot construct a Worker from a URL), and
// a blob: script has no useful base to resolve against — so such a caller sends
// the absolute binary URL in `load-start` instead.
const XN_WASM_URL = './pocket-tts-xn.wasm'
let xnWasmUrl = null

const chunksByAsset = new Map()
const completedAssets = new Map()
let model = null
let tokenizer = null
let voiceIndex = 0
let sampleRate = 24_000
const generation = new SpeechGenerationOwnership()
let modelAllocation = null
let expectedAssetBytes = null
let generationTemperature = 0.3
let clonedVoiceSamples = null

function reviewedAssetBytes(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || ![2, 3].includes(Object.keys(value).length)) {
    throw new Error('The speech model manifest is invalid.')
  }
  const result = {}
  const required = Object.hasOwn(value, 'voice') ? REQUIRED_ASSETS : REQUIRED_ASSETS.slice(0, 2)
  if (Object.keys(value).some((id) => !required.includes(id))) {
    throw new Error('The speech model manifest is invalid.')
  }
  for (const id of required) {
    const bytes = value[id]
    if (!Number.isSafeInteger(bytes) || bytes < 1 || bytes > MAX_ASSET_BYTES) {
      throw new Error('The speech model manifest is invalid.')
    }
    result[id] = bytes
  }
  return Object.freeze(result)
}

function post(type, value = {}, transfer = []) {
  globalThis.postMessage({ type, ...value }, transfer)
}

function errorValue(error) {
  return { name: error?.name || 'Error', message: error?.message || 'Speech stopped unexpectedly.' }
}

function acceptChunk(message) {
  const expected = expectedAssetBytes?.[message.assetId]
  if (!expected || !(message.bytes instanceof ArrayBuffer)) {
    throw new Error('The speech cache returned an invalid chunk.')
  }
  const offset = Number(message.offset)
  const state = chunksByAsset.get(message.assetId) || (message.assetId === 'model'
    ? { bytes: modelAllocation?.bytes, received: 0, allocation: modelAllocation }
    : { bytes: new Uint8Array(expected), received: 0 })
  if (!(state.bytes instanceof Uint8Array) || state.bytes.byteLength !== expected) {
    throw new Error(`The ${message.assetId} speech asset could not be allocated.`)
  }
  const bytes = new Uint8Array(message.bytes)
  if (!Number.isSafeInteger(offset) || offset !== state.received) {
    throw new Error(`The ${message.assetId} speech chunks arrived out of order.`)
  }
  if (offset + bytes.byteLength > expected) {
    throw new Error(`The ${message.assetId} speech asset is larger than expected.`)
  }
  // Fill the final asset buffer directly. Keeping every transferred chunk and
  // joining them later briefly duplicated the 146 MB model during opening.
  state.bytes.set(bytes, offset)
  state.received += bytes.byteLength
  if (state.received === expected) {
    chunksByAsset.delete(message.assetId)
    completedAssets.set(message.assetId, state.allocation || state.bytes)
  } else chunksByAsset.set(message.assetId, state)
}

function decodeSentencepieceModel(input) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input)
  let cursor = 0
  const readVarint = (start = cursor) => {
    let value = 0
    let shift = 0
    let next = start
    while (next < bytes.length) {
      const byte = bytes[next++]
      value |= (byte & 0x7f) << shift
      if ((byte & 0x80) === 0) return { value, cursor: next }
      shift += 7
    }
    return { value, cursor: next }
  }
  const pieces = []
  while (cursor < bytes.length) {
    let field = readVarint(); cursor = field.cursor
    const number = field.value >>> 3
    const wire = field.value & 7
    if (number !== 1 || wire !== 2) {
      if (wire === 0) cursor = readVarint().cursor
      else if (wire === 1) cursor += 8
      else if (wire === 2) { field = readVarint(); cursor = field.cursor + field.value }
      else if (wire === 5) cursor += 4
      else break
      continue
    }
    const length = readVarint(); cursor = length.cursor
    const end = cursor + length.value
    let piece = ''
    let score = 0
    let type = 1
    while (cursor < end) {
      field = readVarint(); cursor = field.cursor
      const nestedNumber = field.value >>> 3
      const nestedWire = field.value & 7
      if (nestedNumber === 1 && nestedWire === 2) {
        const valueLength = readVarint(); cursor = valueLength.cursor
        piece = new TextDecoder().decode(bytes.subarray(cursor, cursor + valueLength.value))
        cursor += valueLength.value
      } else if (nestedNumber === 2 && nestedWire === 5) {
        score = new DataView(bytes.buffer, bytes.byteOffset + cursor, 4).getFloat32(0, true)
        cursor += 4
      } else if (nestedNumber === 3 && nestedWire === 0) {
        const value = readVarint(); type = value.value; cursor = value.cursor
      } else if (nestedWire === 0) cursor = readVarint().cursor
      else if (nestedWire === 1) cursor += 8
      else if (nestedWire === 2) { const value = readVarint(); cursor = value.cursor + value.value }
      else if (nestedWire === 5) cursor += 4
      else break
    }
    cursor = end
    pieces.push({ piece, score, type })
  }
  return pieces
}

class UnigramTokenizer {
  constructor(pieces) {
    this.vocab = new Map()
    this.unknownId = 0
    pieces.forEach((piece, index) => {
      if (piece.type === 2) this.unknownId = index
      if (piece.type === 1 || piece.type === 4 || piece.type === 6) {
        this.vocab.set(piece.piece, { id: index, score: piece.score })
      }
    })
  }

  encode(text) {
    const normalized = `▁${text.replace(/ /g, '▁')}`
    const best = Array.from({ length: normalized.length + 1 }, () => ({ score: -Infinity, length: 0, id: -1 }))
    best[0] = { score: 0, length: 0, id: -1 }
    for (let index = 0; index < normalized.length; index += 1) {
      if (best[index].score === -Infinity) continue
      for (let length = 1; length <= Math.min(64, normalized.length - index); length += 1) {
        const entry = this.vocab.get(normalized.slice(index, index + length))
        if (!entry) continue
        const score = best[index].score + entry.score
        if (score > best[index + length].score) best[index + length] = { score, length, id: entry.id }
      }
      if (best[index + 1].score === -Infinity) {
        best[index + 1] = { score: best[index].score - 100, length: 1, id: this.unknownId }
      }
    }
    const ids = []
    for (let index = normalized.length; index > 0;) {
      const entry = best[index]
      if (!entry.length) break
      ids.push(entry.id)
      index -= entry.length
    }
    return new Uint32Array(ids.reverse())
  }
}

async function prepareRuntime() {
  try {
    // Idempotent: wasm-bindgen's init returns the existing instance once bound.
    await initXnRuntime(xnWasmUrl || new URL(XN_WASM_URL, globalThis.location.href))
  } catch (error) {
    // A CSP that forbids WebAssembly and a browser lacking the SIMD
    // instructions Pocket TTS needs both surface as a CompileError. Naming
    // either as the cause would be a guess, so keep the browser's own reason.
    const detail = error?.message || String(error)
    throw new Error(/content security policy/i.test(detail)
      ? 'This page\'s security policy does not allow WebAssembly, so the speech engine cannot start.'
      : `The speech engine could not start: ${detail}`)
  }
  modelAllocation = allocateModelWeights(expectedAssetBytes.model)
}

async function finishLoad() {
  const required = clonedVoiceSamples ? REQUIRED_ASSETS.slice(0, 2) : REQUIRED_ASSETS
  if (chunksByAsset.size || required.some((id) => !completedAssets.has(id))) {
    throw new Error('The saved speech model is incomplete.')
  }
  post('load-progress', { stage: 'preparing' })
  tokenizer = new UnigramTokenizer(decodeSentencepieceModel(completedAssets.get('tokenizer')))
  model = modelFromAllocatedWeights(
    completedAssets.get('model'),
    'q8',
    Boolean(clonedVoiceSamples),
  )
  modelAllocation = null
  voiceIndex = clonedVoiceSamples
    ? model.clone_voice(clonedVoiceSamples)
    : model.add_voice(completedAssets.get('voice'))
  clonedVoiceSamples = null
  sampleRate = model.sample_rate()
  completedAssets.clear()
  post('load-complete', { backend: 'wasm-xn-q8-worker', sampleRate })
}

async function generate(text, requestId) {
  if (!model || !tokenizer) throw new Error('The speech reader is not ready.')
  generation.claim(requestId)
  let steps = 0
  try {
    const [processedText, rawFramesAfterEos] = model.prepare_text(text)
    // prepare_text returns Kyutai's default post-EOS tail: 1 frame (80 ms) for
    // >4-word blocks (3 for shorter). Measured against the Alba voice, that 80 ms
    // clips the final phoneme mid-decay — a long block's audio ended at full
    // ~0.017 RMS instead of reaching silence, i.e. "eats the end of each block".
    // A downstream boundary trimmer dropped 0 ms because the raw audio remained
    // audible to the very end, confirming generation itself was short. Render the
    // decay so the phoneme completes. Consumers that need tighter timing may trim
    // the resulting quiet tail at their own playback boundary.
    // 1 frame = 80 ms at the 12.5 Hz mimi frame rate; 6 frames ≈ 480 ms.
    const framesAfterEos = completePostEosFrames(rawFramesAfterEos)
    const tokens = tokenizer.encode(processedText)
    model.start_generation(voiceIndex, tokens, framesAfterEos, generationTemperature)
    // Reference guardrail (pocket_tts tts_model.py): a generation should last at
    // most (tokens / 3 + 2) seconds of audio. Past that the decoder is no longer
    // tracking the text — it has failed to emit EOS and is sustaining a resonant
    // tail. Stop there instead of letting it ramble. tokens/sec estimate = 3,
    // padding = 2 s. Expressed in samples so it is frame-rate independent.
    const tokenCount = tokens.length || 1
    const maxSamples = Math.ceil((tokenCount / 3 + 2) * sampleRate)
    let emittedSamples = 0
    while (generation.owns(requestId)) {
      const chunk = model.generation_step()
      if (!chunk) break
      emittedSamples += chunk.length
      post('audio', { requestId, samples: chunk }, [chunk.buffer])
      if (emittedSamples >= maxSamples) break
      steps += 1
      // Yield between small groups of model frames so cancellation and worker
      // control messages are observed without involving the page thread.
      if (steps % 4 === 0) await new Promise((resolve) => setTimeout(resolve, 0))
    }
    if (!generation.owns(requestId)) return
    post('generate-complete', { requestId })
  } catch (error) {
    if (generation.owns(requestId)) post('generate-error', { requestId, error: errorValue(error) })
  } finally {
    // A cancelled request may still be unwinding after its successor starts.
    // Only the request that still owns the generation lease may release it.
    generation.release(requestId)
  }
}

globalThis.onmessage = ({ data: message }) => {
  if (!message || typeof message !== 'object') return
  if (message.type === 'cancel-generate') {
    generation.cancel()
    return
  }
  if (message.type === 'dispose') {
    generation.cancel()
    try { model?.free?.() } catch {}
    try { freeAllocatedModelWeights(modelAllocation) } catch {}
    globalThis.close()
    return
  }
  Promise.resolve().then(async () => {
    if (message.type === 'load-start') {
      xnWasmUrl = typeof message.wasmUrl === 'string' && message.wasmUrl ? message.wasmUrl : null
      generationTemperature = Number.isFinite(message.temperature)
        ? Math.max(0.05, Math.min(1.5, message.temperature))
        : 0.3
      expectedAssetBytes = reviewedAssetBytes(message.assetBytes)
      clonedVoiceSamples = message.clonedVoiceSamples instanceof Float32Array
        ? message.clonedVoiceSamples
        : null
      await prepareRuntime()
      post('load-ready')
    }
    else if (message.type === 'asset-chunk') {
      acceptChunk(message)
      post('chunk-accepted', { chunkId: message.chunkId })
    } else if (message.type === 'load-finish') await finishLoad()
    else if (message.type === 'generate') await generate(message.text, message.requestId)
  }).catch((error) => post('worker-error', { requestId: message.requestId, error: errorValue(error) }))
}
