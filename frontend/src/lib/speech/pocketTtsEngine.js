import { POCKET_TTS_WORKER_SOURCE } from './pocketTtsWorkerSource.js'
import { XN_PTTS_MODULE_SOURCE, XN_PTTS_WASM_BYTES } from './pocketTtsXnModule.js'
import { XN_PTTS_WASM_BASE64_1 } from './pocketTtsXnWasm1.js'
import { XN_PTTS_WASM_BASE64_2 } from './pocketTtsXnWasm2.js'
import { streamSpeechModel } from './speechModelStore.js'

const START_TIMEOUT_MS = 20_000
const CHUNK_TIMEOUT_MS = 180_000
let sharedEngine = null

function abortError() { return new DOMException('Aborted', 'AbortError') }
function restoredError(value, fallback = 'Speech stopped unexpectedly.') {
  const error = new Error(value?.message || fallback)
  error.name = value?.name || 'Error'
  return error
}
function within(promise, milliseconds, message) {
  let timer
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), milliseconds) }),
  ]).finally(() => clearTimeout(timer))
}

export class PocketTtsWorkerRuntime {
  constructor() {
    this.worker = null
    this.workerUrl = ''
    this.loadPending = null
    this.chunks = new Map()
    this.generations = new Map()
    this.nextChunkId = 1
    this.nextRequestId = 1
  }

  ensureWorker() {
    if (this.worker) return this.worker
    if (typeof Worker === 'undefined' || typeof Blob === 'undefined') {
      throw new Error('This browser cannot run the speech model away from the page.')
    }
    this.workerUrl = URL.createObjectURL(new Blob([POCKET_TTS_WORKER_SOURCE], { type: 'text/javascript' }))
    // Keep the outer bootstrap classic so the same runtime works from both the
    // shell and sandboxed app hosts. The XN Wasm-bindgen module is still
    // imported inside the worker after it starts.
    const worker = new Worker(this.workerUrl)
    worker.onmessage = (event) => this.onMessage(event.data)
    worker.onerror = (event) => this.failAll(new Error(event.message || 'The speech worker stopped.'))
    this.worker = worker
    return worker
  }

  onMessage(message) {
    if (!message || typeof message !== 'object') return
    if (message.type === 'load-ready') {
      this.loadPending?.readyResolve?.()
      return
    }
    if (message.type === 'load-progress') {
      this.loadPending?.onProgress?.({ stage: 'preparing', percent: this.loadPending.readPercent || 96 })
      return
    }
    if (message.type === 'chunk-accepted') {
      const pending = this.chunks.get(message.chunkId)
      this.chunks.delete(message.chunkId)
      pending?.resolve()
      return
    }
    if (message.type === 'load-complete') {
      const pending = this.loadPending
      this.loadPending = null
      if (this.workerUrl) URL.revokeObjectURL(this.workerUrl)
      this.workerUrl = ''
      pending?.resolve()
      return
    }
    if (message.type === 'audio') {
      const generation = this.generations.get(message.requestId)
      if (!generation) return
      Promise.resolve(generation.onChunk?.(message.samples)).catch((error) => {
        this.worker?.postMessage({ type: 'cancel-generate' })
        this.generations.delete(message.requestId)
        generation.cleanup()
        generation.reject(error)
      })
      return
    }
    if (message.type === 'generate-complete') {
      const generation = this.generations.get(message.requestId)
      this.generations.delete(message.requestId)
      generation?.cleanup()
      generation?.resolve()
      return
    }
    if (message.type === 'generate-error') {
      const generation = this.generations.get(message.requestId)
      this.generations.delete(message.requestId)
      generation?.cleanup()
      generation?.reject(restoredError(message.error))
      return
    }
    if (message.type === 'worker-error') {
      const error = restoredError(message.error)
      const generation = message.requestId && this.generations.get(message.requestId)
      if (generation) {
        this.generations.delete(message.requestId)
        generation.cleanup()
        generation.reject(error)
      } else this.failAll(error)
    }
  }

  failAll(error) {
    this.loadPending?.readyReject?.(error)
    this.loadPending?.reject?.(error)
    this.loadPending = null
    for (const pending of this.chunks.values()) pending.reject(error)
    this.chunks.clear()
    for (const generation of this.generations.values()) {
      generation.cleanup()
      generation.reject(error)
    }
    this.generations.clear()
  }

  sendChunk(value) {
    const chunkId = this.nextChunkId++
    if (!(value?.bytes instanceof ArrayBuffer)) {
      return Promise.reject(new Error('The speech cache returned an invalid chunk.'))
    }
    const accepted = new Promise((resolve, reject) => this.chunks.set(chunkId, { resolve, reject }))
    this.worker.postMessage({
      type: 'asset-chunk',
      chunkId,
      assetId: value.assetId,
      index: value.index,
      offset: value.offset,
      bytes: value.bytes,
    }, [value.bytes])
    return within(accepted, CHUNK_TIMEOUT_MS, 'The speech worker took too long to open the saved model.')
  }

  async load({ modelId, signal, onProgress } = {}) {
    if (this.loadPending) throw new Error('The speech model is already loading.')
    const worker = this.ensureWorker()
    let resolveLoad; let rejectLoad; let readyResolve; let readyReject
    const loaded = new Promise((resolve, reject) => { resolveLoad = resolve; rejectLoad = reject })
    const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject })
    loaded.catch(() => {}); ready.catch(() => {})
    this.loadPending = { resolve: resolveLoad, reject: rejectLoad, readyResolve, readyReject, onProgress, readPercent: 0 }
    const cancel = () => { this.dispose(); rejectLoad(abortError()) }
    signal?.addEventListener('abort', cancel, { once: true })
    try {
      onProgress?.({ stage: 'starting', percent: 0 })
      worker.postMessage({
        type: 'load-start',
        runtimeModuleSource: XN_PTTS_MODULE_SOURCE,
        runtimeWasmBase64Parts: [XN_PTTS_WASM_BASE64_1, XN_PTTS_WASM_BASE64_2],
        runtimeWasmBytes: XN_PTTS_WASM_BYTES,
      })
      await within(ready, START_TIMEOUT_MS, 'The speech worker did not start.')
      onProgress?.({ stage: 'checking', percent: 0 })
      await streamSpeechModel(modelId, {
        signal,
        onChunk: (value) => this.sendChunk(value),
        onProgress: (percent) => {
          if (this.loadPending) this.loadPending.readPercent = percent
          onProgress?.({ stage: 'reading', percent })
        },
      })
      onProgress?.({ stage: 'preparing', percent: 97 })
      worker.postMessage({ type: 'load-finish' })
      return await loaded
    } catch (error) {
      this.dispose()
      throw error
    } finally { signal?.removeEventListener('abort', cancel) }
  }

  generate(text, { signal, onChunk } = {}) {
    if (signal?.aborted) return Promise.reject(abortError())
    const requestId = `speech-${this.nextRequestId++}`
    return new Promise((resolve, reject) => {
      const cancel = () => {
        this.worker?.postMessage({ type: 'cancel-generate' })
        this.generations.delete(requestId)
        reject(abortError())
      }
      signal?.addEventListener('abort', cancel, { once: true })
      this.generations.set(requestId, {
        resolve, reject, onChunk,
        cleanup: () => signal?.removeEventListener('abort', cancel),
      })
      this.worker.postMessage({ type: 'generate', requestId, text })
    })
  }

  dispose() {
    const error = abortError()
    try { this.worker?.postMessage({ type: 'dispose' }) } catch {}
    this.worker?.terminate()
    this.worker = null
    if (this.workerUrl) URL.revokeObjectURL(this.workerUrl)
    this.workerUrl = ''
    this.failAll(error)
  }
}

class BrowserPocketTts {
  constructor() {
    this.runtime = new PocketTtsWorkerRuntime()
    this.loaded = false
    this.loading = null
    this.modelId = null
  }
  async load(options = {}) {
    if (this.loaded && this.modelId === options.modelId) return
    if (this.loaded && this.modelId !== options.modelId) this.dispose()
    if (!this.loading) this.loading = this.runtime.load(options).then(() => {
      this.loaded = true
      this.modelId = options.modelId
    }).finally(() => { this.loading = null })
    return this.loading
  }
  generate(text, options) {
    if (!this.loaded) throw new Error('The speech model is not ready.')
    return this.runtime.generate(text, options)
  }
  dispose() {
    this.runtime.dispose()
    this.loaded = false
    this.loading = null
    this.modelId = null
  }
}

export function browserSpeechEngine() {
  if (!sharedEngine) sharedEngine = new BrowserPocketTts()
  return sharedEngine
}

export function releaseBrowserSpeechEngine() {
  sharedEngine?.dispose()
  sharedEngine = null
}
