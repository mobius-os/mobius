import { SpeechGenerationOwnership } from './speechGenerationOwnership.js'

// XN's Q8 Pocket TTS runtime, isolated in a dedicated worker. Model assets
// arrive from Möbius's checksum-verified device cache; the worker never fetches.

const ASSET_BYTES = Object.freeze({
  // Keep the original runtime entries in the package handshake so existing
  // 154 MB XN downloads remain valid. The reader uses the app-bundled,
  // baseline-SIMD runtime below; these two small cached values are ignored.
  'runtime-module': 12_706,
  'runtime-wasm': 952_895,
  tokenizer: 59_339,
  model: 146_499_264,
  voice: 6_148_328,
})

const chunksByAsset = new Map()
const completedAssets = new Map()
let runtimeModuleUrl = ''
let model = null
let tokenizer = null
let voiceIndex = 0
let sampleRate = 24_000
const generation = new SpeechGenerationOwnership()
let embeddedRuntime = null

function decodeEmbeddedWasm(parts, expectedBytes) {
  const bytes = new Uint8Array(expectedBytes)
  let offset = 0
  for (const part of parts) {
    const decoded = globalThis.atob(part)
    for (let index = 0; index < decoded.length; index += 1) {
      bytes[offset + index] = decoded.charCodeAt(index)
    }
    offset += decoded.length
  }
  if (offset !== expectedBytes) {
    throw new Error('The built-in speech reader is incomplete.')
  }
  return bytes
}

function post(type, value = {}, transfer = []) {
  globalThis.postMessage({ type, ...value }, transfer)
}

function errorValue(error) {
  return { name: error?.name || 'Error', message: error?.message || 'Speech stopped unexpectedly.' }
}

function acceptChunk(message) {
  const expected = ASSET_BYTES[message.assetId]
  if (!expected || !(message.bytes instanceof ArrayBuffer)) {
    throw new Error('The speech cache returned an invalid chunk.')
  }
  const offset = Number(message.offset)
  const state = chunksByAsset.get(message.assetId) || {
    bytes: new Uint8Array(expected),
    received: 0,
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
    completedAssets.set(message.assetId, state.bytes)
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

async function finishLoad() {
  if (chunksByAsset.size || Object.keys(ASSET_BYTES).some((id) => !completedAssets.has(id))) {
    throw new Error('The saved speech model is incomplete.')
  }
  if (!embeddedRuntime) throw new Error('The built-in speech reader is missing.')
  post('load-progress', { stage: 'preparing' })
  runtimeModuleUrl = URL.createObjectURL(new Blob([embeddedRuntime.moduleSource], { type: 'text/javascript' }))
  const runtime = await import(runtimeModuleUrl)
  const wasmBytes = decodeEmbeddedWasm(embeddedRuntime.wasmBase64Parts, embeddedRuntime.wasmBytes)
  embeddedRuntime = null
  if (!WebAssembly.validate(wasmBytes)) {
    throw new Error('This browser needs WebAssembly SIMD to use listening.')
  }
  const wasmModule = await WebAssembly.compile(wasmBytes)
  await runtime.default(wasmModule)
  tokenizer = new UnigramTokenizer(decodeSentencepieceModel(completedAssets.get('tokenizer')))
  model = new runtime.Model(completedAssets.get('model'), 'q8')
  voiceIndex = model.add_voice(completedAssets.get('voice'))
  sampleRate = model.sample_rate()
  completedAssets.clear()
  URL.revokeObjectURL(runtimeModuleUrl)
  runtimeModuleUrl = ''
  post('load-complete', { backend: 'wasm-xn-q8-worker', sampleRate })
}

async function generate(text, requestId) {
  if (!model || !tokenizer) throw new Error('The speech reader is not ready.')
  generation.claim(requestId)
  let steps = 0
  try {
    const [processedText, framesAfterEos] = model.prepare_text(text)
    model.start_generation(voiceIndex, tokenizer.encode(processedText), framesAfterEos, 0.7)
    while (generation.owns(requestId)) {
      const chunk = model.generation_step()
      if (!chunk) break
      post('audio', { requestId, samples: chunk }, [chunk.buffer])
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
    if (runtimeModuleUrl) URL.revokeObjectURL(runtimeModuleUrl)
    globalThis.close()
    return
  }
  Promise.resolve().then(async () => {
    if (message.type === 'load-start') {
      const parts = message.runtimeWasmBase64Parts
      if (typeof message.runtimeModuleSource !== 'string'
        || !Array.isArray(parts)
        || parts.length !== 2
        || parts.some((part) => typeof part !== 'string')
        || !Number.isSafeInteger(message.runtimeWasmBytes)) {
        throw new Error('The built-in speech reader is missing.')
      }
      embeddedRuntime = {
        moduleSource: message.runtimeModuleSource,
        wasmBase64Parts: parts,
        wasmBytes: message.runtimeWasmBytes,
      }
      post('load-ready')
    }
    else if (message.type === 'asset-chunk') {
      acceptChunk(message)
      post('chunk-accepted', { chunkId: message.chunkId })
    } else if (message.type === 'load-finish') await finishLoad()
    else if (message.type === 'generate') await generate(message.text, message.requestId)
  }).catch((error) => post('worker-error', { requestId: message.requestId, error: errorValue(error) }))
}
