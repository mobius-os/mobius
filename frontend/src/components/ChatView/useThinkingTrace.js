import { useEffect, useRef, useState } from 'react'
import { fetchLazyText } from './lazySidecar.js'

// Backward-compatible names for the small pure retry contract's focused unit
// test. Tool and thought sidecars now share the same bounded policy.
export {
  MAX_PENDING_SIDECAR_RETRIES as MAX_PENDING_TRACE_RETRIES,
  pendingSidecarRetryDelay as pendingTraceRetryDelay,
} from './lazySidecar.js'

/** Fetch a deferred thought only while its own nested disclosure is open.
 * Closing aborts in-flight work and releases the loaded string from memory. */
export function useThinkingTrace({ open, thought, chatId }) {
  const deferred = !!thought.thinking_deferred
  const [loadedContent, setLoadedContent] = useState('')
  const [loadState, setLoadState] = useState('idle')
  const [previewComplete, setPreviewComplete] = useState(true)
  const [traceComplete, setTraceComplete] = useState(false)
  const [fullRequested, setFullRequested] = useState(false)
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
    const timer = setTimeout(() => {
      // A live trace that changes after an explicit full load returns to the
      // bounded preview. Otherwise each token burst would redownload the full
      // growing Markdown payload.
      setFullRequested(false)
      setRefreshNonce(value => value + 1)
    }, 450)
    return () => clearTimeout(timer)
  }, [open, deferred, thought.thinking_revision])

  useEffect(() => {
    if (!open || !deferred || !chatId || !thought.thinking_id) {
      if (!open && deferred) {
        setLoadedContent('')
        setLoadState('idle')
        setPreviewComplete(true)
        setTraceComplete(false)
        setFullRequested(false)
      }
      return
    }
    const controller = new AbortController()
    let cancelled = false
    const url = `/chats/${chatId}/thinking-trace/${encodeURIComponent(thought.thinking_id)}`
      + `?revision=${revisionRef.current}`
      + (fullRequested ? '' : '&preview=1')

    setLoadState('loading')
    fetchLazyText(url, { signal: controller.signal })
      .then(({ response, text }) => {
        if (!cancelled) {
          setLoadedContent(text)
          setPreviewComplete(
            fullRequested
            || response.headers.get('X-Thinking-Preview-Complete') !== '0',
          )
          setTraceComplete(response.headers.get('X-Thinking-Complete') === '1')
          setLoadState('ready')
        }
      })
      .catch(error => {
        if (!cancelled && error?.name !== 'AbortError') setLoadState('failed')
      })
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [open, deferred, chatId, thought.thinking_id, fullRequested, refreshNonce])

  return {
    content: deferred ? loadedContent : (thought.content || ''),
    loadState,
    previewComplete: !deferred || previewComplete,
    // Persisted snapshots carry this flag, and final live-stream promotion
    // stamps it locally. This makes the explicit full-load action available
    // at completion without another request; the payload remains lazy.
    traceComplete: !deferred || traceComplete || !!thought.thinking_complete,
    loadFull: () => {
      setFullRequested(true)
      setLoadState('loading')
    },
    retry: () => {
      setLoadedContent('')
      setLoadState('loading')
      setRefreshNonce(value => value + 1)
    },
  }
}
