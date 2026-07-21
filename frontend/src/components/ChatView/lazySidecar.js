import { apiFetch } from '../../api/client.js'

export const MAX_PENDING_SIDECAR_RETRIES = 5
const DEFAULT_RETRY_MS = 1000
const MIN_RETRY_MS = 250
const MAX_RETRY_MS = 5000

/** Prefer the server's retry window and fall back to capped exponential
 * backoff, so a pending writer never creates a tight polling loop. */
export function pendingSidecarRetryDelay(retryAfter, retryNumber) {
  const seconds = retryAfter == null || retryAfter === '' ? NaN : Number(retryAfter)
  if (Number.isFinite(seconds) && seconds >= 0) {
    return Math.max(MIN_RETRY_MS, Math.min(seconds * 1000, MAX_RETRY_MS))
  }
  const parsedRetry = Number(retryNumber)
  const attempt = Math.max(0, (Number.isFinite(parsedRetry) ? parsedRetry : 1) - 1)
  return Math.min(DEFAULT_RETRY_MS * (2 ** attempt), MAX_RETRY_MS)
}

function abortableDelay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const timer = setTimeout(done, ms)
    signal?.addEventListener('abort', aborted, { once: true })

    function cleanup() {
      clearTimeout(timer)
      signal?.removeEventListener('abort', aborted)
    }
    function done() {
      cleanup()
      resolve()
    }
    function aborted() {
      cleanup()
      reject(abortError())
    }
  })
}

function abortError() {
  const error = new Error('Aborted')
  error.name = 'AbortError'
  return error
}

/** Fetch a lazy text sidecar with bounded handling for the server's 202
 * "writer still pending" response. The caller owns the AbortSignal. */
export async function fetchLazyText(url, { signal } = {}) {
  let pendingRetries = 0
  while (true) {
    const response = await apiFetch(url, { signal })
    if (response.status !== 202) {
      if (!response.ok) {
        const error = new Error(`HTTP ${response.status}`)
        error.status = response.status
        throw error
      }
      return { response, text: await response.text() }
    }
    if (pendingRetries >= MAX_PENDING_SIDECAR_RETRIES) {
      throw new Error('Sidecar is still pending')
    }
    pendingRetries += 1
    await abortableDelay(
      pendingSidecarRetryDelay(
        response.headers.get('Retry-After'),
        pendingRetries,
      ),
      signal,
    )
  }
}
