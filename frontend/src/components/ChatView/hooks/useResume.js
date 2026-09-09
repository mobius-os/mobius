/* Own the acknowledged Resume transaction, independently of the composer and queue. */
import { useCallback, useEffect, useRef, useState } from 'react'

export default function useResume({ chatId, runId, send, onAccepted, onRefresh, blocked }) {
  const [state, setState] = useState({ pending: false, error: '' })
  const attemptRef = useRef(null)
  const scopeRef = useRef(null)

  useEffect(() => {
    const scope = { chatId }
    scopeRef.current = scope
    attemptRef.current = null
    setState({ pending: false, error: '' })
    return () => { if (scopeRef.current === scope) scopeRef.current = null }
  }, [chatId, runId])

  const resume = useCallback(async () => {
    if (blocked?.() || attemptRef.current?.pending) return false
    if (!runId && !attemptRef.current) {
      setState({ pending: false, error: 'Recovery details are not available yet. Try again once Möbius reconnects.' })
      onRefresh()
      return false
    }
    const scope = scopeRef.current
    const attempt = attemptRef.current || { cid: crypto.randomUUID(), runId }
    attempt.pending = true
    attemptRef.current = attempt
    setState({ pending: true, error: '' })
    try {
      const result = await send('continue', undefined, {
        cid: attempt.cid,
        continuation: 'manual',
        resumeRunId: attempt.runId || undefined,
      })
      if (scopeRef.current !== scope) return false
      attemptRef.current = null
      // No optimistic continuation: only the server's accepted durable row
      // can supersede the recovery card or mark the turn as resumed.
      onAccepted(result)
      setState({ pending: false, error: '' })
      return true
    } catch (error) {
      if (scopeRef.current !== scope) return false
      // Retain the identity for an ambiguous retry, including a durable outbox
      // replay. A rejected request never creates a new transcript/queue row.
      attempt.pending = false
      if (Number(error?.status) >= 400 && Number(error?.status) < 500
          && !error?.outboxRetained) attemptRef.current = null
      setState({
        pending: false,
        error: error?.code === 'recovery_changed'
          ? 'Recovery state changed. Refreshing the chat…'
          : error?.code === 'pending_question_open'
            ? 'Answer the waiting question to continue.'
            : error?.code === 'model_selection_required'
              ? 'Choose a model before resuming.'
              : error?.outboxRetained
          ? 'Resume is saved and will retry when Möbius reconnects.'
          : 'Could not confirm Resume. Your draft and queued messages are unchanged. Try again.',
      })
      return false
    } finally {
      // Runtime may observe the successor before this acknowledgement. The
      // old action cannot update presentation, but the same mounted chat must
      // still reconcile its durable marker; a chat switch/unmount must not.
      if (scopeRef.current?.chatId === chatId) onRefresh()
    }
  }, [chatId, runId, send, onAccepted, onRefresh, blocked])

  return { resume, state }
}
