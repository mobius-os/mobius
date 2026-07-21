import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'

export const MAX_PENDING_TRACE_RETRIES = 5
const DEFAULT_RETRY_MS = 1000
const MIN_RETRY_MS = 250
const MAX_RETRY_MS = 5000

/** Prefer the server's retry window. If an older server omits it, back off
 * locally instead of falling into a tight fixed poll. */
export function pendingTraceRetryDelay(retryAfter, retryNumber) {
  const seconds = retryAfter == null || retryAfter === '' ? NaN : Number(retryAfter)
  if (Number.isFinite(seconds) && seconds >= 0) {
    return Math.max(MIN_RETRY_MS, Math.min(seconds * 1000, MAX_RETRY_MS))
  }
  const parsedRetry = Number(retryNumber)
  const attempt = Math.max(0, (Number.isFinite(parsedRetry) ? parsedRetry : 1) - 1)
  return Math.min(DEFAULT_RETRY_MS * (2 ** attempt), MAX_RETRY_MS)
}

/** Fetch a deferred thought only while its own nested disclosure is open.
 * Closing aborts in-flight work and releases the loaded string from memory. */
export function useThinkingTrace({ open, thought, chatId }) {
  const deferred = !!thought.thinking_deferred
  const [loadedContent, setLoadedContent] = useState('')
  const [loadState, setLoadState] = useState('idle')
  const [refreshNonce, setRefreshNonce] = useState(0)
  const revisionRef = useRef(Number(thought.thinking_revision) || 0)
  revisionRef.current = Number(thought.thinking_revision) || 0
  const debouncedRevisionRef = useRef(revisionRef.current)

  // Reasoning metadata can update once per token. Restarting a GET on every
  // revision creates an abort/request storm, so only schedule a refresh after
  // the stream has been quiet for a moment. Opening still fetches immediately.
  useEffect(() => {
    if (!open || !deferred) {
      debouncedRevisionRef.current = revisionRef.current
      return
    }
    if (debouncedRevisionRef.current === revisionRef.current) return
    debouncedRevisionRef.current = revisionRef.current
    const timer = setTimeout(() => setRefreshNonce(value => value + 1), 450)
    return () => clearTimeout(timer)
  }, [open, deferred, thought.thinking_revision])

  useEffect(() => {
    if (!open || !deferred || !chatId || !thought.thinking_id) {
      if (!open && deferred) {
        setLoadedContent('')
        setLoadState('idle')
      }
      return
    }
    const controller = new AbortController()
    let retryTimer = null
    let cancelled = false
    let pendingRetries = 0
    const url = `/chats/${chatId}/thinking-trace/${encodeURIComponent(thought.thinking_id)}`
      + `?revision=${revisionRef.current}`

    const load = () => {
      setLoadState('loading')
      apiFetch(url, { signal: controller.signal })
        .then(async res => {
          if (res.status === 202) {
            if (pendingRetries >= MAX_PENDING_TRACE_RETRIES) {
              throw new Error('Thinking trace is still pending')
            }
            pendingRetries += 1
            retryTimer = setTimeout(
              load,
              pendingTraceRetryDelay(res.headers.get('Retry-After'), pendingRetries),
            )
            return null
          }
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          return res.text()
        })
        .then(text => {
          if (text != null && !cancelled) {
            setLoadedContent(text)
            setLoadState('ready')
          }
        })
        .catch(error => {
          if (!cancelled && error?.name !== 'AbortError') setLoadState('failed')
        })
    }
    load()
    return () => {
      cancelled = true
      clearTimeout(retryTimer)
      controller.abort()
    }
  }, [open, deferred, chatId, thought.thinking_id, refreshNonce])

  return {
    content: deferred ? loadedContent : (thought.content || ''),
    loadState,
    retry: () => {
      setLoadedContent('')
      setLoadState('loading')
      setRefreshNonce(value => value + 1)
    },
  }
}
