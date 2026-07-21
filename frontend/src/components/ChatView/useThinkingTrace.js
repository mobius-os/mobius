import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'

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
    const url = `/chats/${chatId}/thinking-trace/${encodeURIComponent(thought.thinking_id)}`
      + `?revision=${revisionRef.current}`

    const load = () => {
      setLoadState('loading')
      apiFetch(url, { signal: controller.signal })
        .then(async res => {
          if (res.status === 202) {
            retryTimer = setTimeout(load, 650)
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
  }
}
