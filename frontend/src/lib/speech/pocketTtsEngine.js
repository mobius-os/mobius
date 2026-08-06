import { streamSpeechModel } from './speechModelStore.js'
import { speechModelLoadSnapshot } from './speechModels.js'
import { SPEECH_WORKER_URL } from './speechWorkerAsset.js'

const START_TIMEOUT_MS = 20_000
const CHUNK_TIMEOUT_MS = 180_000
let sharedEngine = null

function abortError() { return new DOMException('Aborted', 'AbortError') }
function restoredError(value, fallback = 'Speech stopped unexpectedly.') {
  const error = new Error(value?.message || fallback)
  error.name = value?.name || 'Error'
  return error
}
// A worker `error` event only carries a message when the script ran and threw.
// When the script never loaded — blocked by Content-Security-Policy, missing
// from the build, or unreachable — browsers fire a bare Event with no message.
// Those are different failures, so never report one as the other.
function workerStartError(event) {
  return new Error(event?.message
    || `The speech engine could not start: ${SPEECH_WORKER_URL} did not load. `
      + 'It is missing from this build or blocked by this page\'s security policy.')
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
    this.loadPending = null
    this.chunks = new Map()
    this.generations = new Map()
    this.nextChunkId = 1
    this.nextRequestId = 1
  }

  ensureWorker() {
    if (this.worker) return this.worker
    if (typeof Worker === 'undefined') {
      throw new Error('This browser cannot run the speech model away from the page.')
    }
    // Classic worker: the XN runtime is bundled into this script at build time,
    // so it needs neither module-worker support nor a dynamic import.
    const worker = new Worker(SPEECH_WORKER_URL)
    worker.onmessage = (event) => this.onMessage(event.data)
    worker.onerror = (event) => {
      if (this.worker !== worker) return
      this.retireWorker(worker)
      this.failAll(workerStartError(event))
    }
    this.worker = worker
    return worker
  }

  retireWorker(worker = this.worker) {
    if (worker && this.worker !== worker) return
    if (worker) {
      worker.onmessage = null
      worker.onerror = null
      try { worker.terminate() } catch {}
    }
    this.worker = null
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
      pending?.resolve()
      return
    }
    if (message.type === 'audio') {
      const generation = this.generations.get(message.requestId)
      if (!generation) return
      try {
        // Audio delivery is synchronous so completion cannot race a rejected
        // consumer promise after the generation has already settled.
        generation.onChunkSync?.(message.samples)
      } catch (error) {
        this.failGeneration(message.requestId, generation, error)
      }
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

  failGeneration(requestId, generation, error) {
    if (this.generations.get(requestId) !== generation) return
    try { this.worker?.postMessage({ type: 'cancel-generate' }) } catch {}
    this.generations.delete(requestId)
    generation.cleanup()
    generation.reject(error)
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

  async load({ snapshot, signal, onProgress } = {}) {
    if (signal?.aborted) throw abortError()
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
        assetBytes: snapshot.assetBytes,
        temperature: snapshot.temperature,
        clonedVoiceSamples: snapshot.clonedVoiceSamples,
      })
      await within(ready, START_TIMEOUT_MS, 'The speech worker did not start.')
      onProgress?.({ stage: 'checking', percent: 0 })
      await streamSpeechModel(snapshot, {
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

  generate(text, { signal, onChunkSync } = {}) {
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
        resolve, reject, onChunkSync,
        cleanup: () => signal?.removeEventListener('abort', cancel),
      })
      this.worker.postMessage({ type: 'generate', requestId, text })
    })
  }

  dispose() {
    const error = abortError()
    try { this.worker?.postMessage({ type: 'dispose' }) } catch {}
    this.retireWorker()
    this.failAll(error)
  }
}

class BrowserPocketTts {
  constructor() {
    this.runtime = new PocketTtsWorkerRuntime()
    this.loaded = false
    this.loading = null
    this.modelId = null
    this.loadIdentity = null
  }
  async load(options = {}) {
    if (this.loading) {
      await this.loading
      return this.load(options)
    }
    const snapshot = speechModelLoadSnapshot(options.modelId, options.storage)
    if (!snapshot) throw new Error('The selected speech model is unavailable.')
    if (this.loaded && this.loadIdentity === snapshot.identity) return
    if (this.loaded) this.dispose()
    if (!this.loading) this.loading = this.runtime.load({
      snapshot,
      signal: options.signal,
      onProgress: options.onProgress,
    }).then(() => {
      this.loaded = true
      this.modelId = snapshot.modelId
      this.loadIdentity = snapshot.identity
    }).finally(() => { this.loading = null })
    return this.loading
  }
  generate(text, { onChunk, ...options } = {}) {
    if (!this.loaded) throw new Error('The speech model is not ready.')
    return this.runtime.generate(text, { ...options, onChunkSync: onChunk })
  }
  dispose() {
    this.runtime.dispose()
    this.loaded = false
    this.loading = null
    this.modelId = null
    this.loadIdentity = null
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
