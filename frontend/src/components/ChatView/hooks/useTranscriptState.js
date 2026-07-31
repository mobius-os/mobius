import { useCallback, useRef, useState } from 'react'
import { sameMessageList } from '../chatMessageList.js'

/**
 * Owns the mounted transcript and its query-cache projection.
 *
 * The ref mirror is advanced synchronously before React state so consecutive
 * mutations in one batch compose against the same authority. Cache publication
 * happens exactly once per mutation and never from inside a React state updater.
 * `applyMessagesToView` deliberately skips publication for an already-published
 * authoritative activation snapshot.
 */
export default function useTranscriptState({ cacheKey, cached, queryClient }) {
  const [messages, setMessages] = useState(() => cached?.messages ?? [])
  const [offset, setOffset] = useState(() => cached?.offset ?? 0)
  const messagesRef = useRef(messages)
  const offsetRef = useRef(offset)
  messagesRef.current = messages
  offsetRef.current = offset

  const applyMessagesToView = useCallback((next, nextOffset, force = false) => {
    const prev = messagesRef.current
    messagesRef.current = next
    if (nextOffset !== undefined) offsetRef.current = nextOffset
    if (!force && sameMessageList(prev, next)) {
      if (nextOffset !== undefined) {
        setOffset(current => current === nextOffset ? current : nextOffset)
      }
      return
    }
    setMessages(next)
    if (nextOffset !== undefined) {
      setOffset(current => current === nextOffset ? current : nextOffset)
    }
  }, [])

  const commitMessages = useCallback((updater, nextOffset, opts) => {
    const force = opts?.force === true
    const prev = messagesRef.current
    const next = typeof updater === 'function' ? updater(prev) : updater
    queryClient.setQueryData(cacheKey, existing => ({
      ...(existing || {}),
      // Local and streamed composites are newer than the last complete detail
      // read, so they cannot retain that response's authoritative version proof.
      updated_at: null,
      messages: next,
      offset: nextOffset !== undefined ? nextOffset : (existing?.offset ?? 0),
    }))
    applyMessagesToView(next, nextOffset, force)
  }, [applyMessagesToView, cacheKey, queryClient])

  return {
    messages,
    messagesRef,
    offset,
    offsetRef,
    applyMessagesToView,
    commitMessages,
  }
}
