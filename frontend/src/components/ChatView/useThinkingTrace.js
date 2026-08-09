import { useEffect, useRef, useState } from 'react'
import { fetchLazyText } from './lazySidecar.js'

// Backward-compatible names for the small pure retry contract's focused unit
// test. Tool and thought sidecars now share the same bounded policy.
export {
  MAX_PENDING_SIDECAR_RETRIES as MAX_PENDING_TRACE_RETRIES,
  pendingSidecarRetryDelay as pendingTraceRetryDelay,
} from './lazySidecar.js'

/** Fetch a deferred thought's FULL text while its disclosure is open.
 *
 * The server keeps the whole thought; the browser only pulls it when you open
 * the block, and drops it on close. There is deliberately no bounded preview
 * and no persistent local copy: expanding shows the complete reasoning, and a
 * thought that is still streaming re-fetches as it grows so you watch it fill
 * in live. The text on screen never blanks — a thought that crosses the inline
 * threshold while open keeps its last inline text as a bridge until the first
 * fetch lands, and a failed/lagging fetch keeps whatever is already shown. */
export function useThinkingTrace({ open, thought, chatId }) {
  const deferred = !!thought.thinking_deferred
  const [loadedContent, setLoadedContent] = useState('')
  const [loadState, setLoadState] = useState('idle')
  const [refreshNonce, setRefreshNonce] = useState(0)
  const revisionRef = useRef(Number(thought.thinking_revision) || 0)
  revisionRef.current = Number(thought.thinking_revision) || 0
  const debouncedRevisionRef = useRef(revisionRef.current)

  // The last inline text seen before the server deferred this thought (it strips
  // the inline copy at the ~1KB cutoff). Held so a thought that crosses that
  // cutoff WHILE OPEN keeps its text on screen instead of blanking to a spinner
  // until the first full fetch resolves.
  const bridgeRef = useRef('')
  if (!deferred && thought.content) bridgeRef.current = thought.content

  // Reasoning metadata can bump once per token. A live thought re-fetches as it
  // grows so you see it stream, but only after a short quiet window so a burst
  // of tokens is one fetch, not dozens. Opening fetches immediately.
  useEffect(() => {
    if (!open || !deferred) {
      debouncedRevisionRef.current = revisionRef.current
      return
    }
    if (debouncedRevisionRef.current === revisionRef.current) return
    debouncedRevisionRef.current = revisionRef.current
    const timer = setTimeout(() => setRefreshNonce(value => value + 1), 350)
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
    let cancelled = false
    // Fetch the FULL current trace. No `preview` (never a bounded preview) and
    // no `revision` pin: asking for an exact revision the server has not written
    // yet returns 202 and makes a live thought hang on "Loading…", so instead
    // take whatever is stored now — the next revision bump re-fetches to catch
    // up.
    const url = `/chats/${chatId}/thinking-trace/${encodeURIComponent(thought.thinking_id)}`

    setLoadState('loading')
    fetchLazyText(url, { signal: controller.signal })
      .then(({ text }) => {
        if (!cancelled) {
          setLoadedContent(text)
          setLoadState('ready')
        }
      })
      .catch(error => {
        // Keep whatever is already on screen (the bridge or a prior fetch) and
        // let the next revision re-fetch recover; a hard error only surfaces on
        // a cold load with nothing to show (see the honest loadState below).
        if (!cancelled && error?.name !== 'AbortError') setLoadState('failed')
      })
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [open, deferred, chatId, thought.thinking_id, refreshNonce])

  const content = deferred
    ? (loadedContent || bridgeRef.current || '')
    : (thought.content || '')

  return {
    content,
    // Honest state: once there is any text to show (a fresh fetch OR the inline
    // bridge), we are 'ready'. The spinner and the error/retry are only for a
    // cold load with nothing on screen, so a background re-fetch (or a transient
    // failure) never blanks a thought you are reading.
    loadState: content ? 'ready' : loadState,
    retry: () => {
      setLoadState('loading')
      setRefreshNonce(value => value + 1)
    },
  }
}
