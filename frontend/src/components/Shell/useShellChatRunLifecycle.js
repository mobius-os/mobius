/* Own Shell's local/durable chat-run reconciliation and monotonic run signals. */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  bumpChatRunSignal,
  chatRunSignal,
} from '../../lib/chatRunSignal.js'
import { withoutSettledLocalChatRuns } from './chatListProjection.js'
import { forgetChatFirstPageRead } from '../../lib/chatDetailReads.js'


export default function useShellChatRunLifecycle(chats = []) {
  // A mounted ChatView publishes a start before the compact chat row can. Keep
  // that optimistic owner synchronous, then retire it only after a system
  // start acknowledged the run and a fresh durable row proves it settled.
  const [localStreamingChatIds, setLocalStreamingChatIds] = useState(
    () => new Set(),
  )
  const localStreamingChatIdsRef = useRef(localStreamingChatIds)
  const acknowledgedLocalChatRunIdsRef = useRef(new Set())

  // System events are activity, not boolean state. Monotonic signals preserve
  // a start+finish pair even when React batches the final running value back to
  // false in one render.
  const [chatRunSignals, setChatRunSignals] = useState(() => new Map())
  const streamingChatIds = useMemo(() => {
    const next = new Set(localStreamingChatIds)
    for (const chat of chats) {
      if (chat.running) next.add(chat.id)
    }
    return next
  }, [localStreamingChatIds, chats])
  const streamingChatIdsRef = useRef(streamingChatIds)
  useEffect(() => {
    streamingChatIdsRef.current = streamingChatIds
  }, [streamingChatIds])

  const markStreamingStart = useCallback((chatId) => {
    if (!chatId) return
    const key = String(chatId)
    // Same-task allocation and placement decisions read the union ref before
    // React can commit the local Set, so publish the optimistic fact to both.
    if (!streamingChatIdsRef.current.has(key)) {
      const next = new Set(streamingChatIdsRef.current)
      next.add(key)
      streamingChatIdsRef.current = next
    }
    const previous = localStreamingChatIdsRef.current
    if (previous.has(key)) return
    const next = new Set(previous)
    next.add(key)
    localStreamingChatIdsRef.current = next
    setLocalStreamingChatIds(next)
  }, [])

  const markStreamingAcknowledged = useCallback((chatId) => {
    if (!chatId) return
    const key = String(chatId)
    acknowledgedLocalChatRunIdsRef.current.add(key)
    markStreamingStart(key)
  }, [markStreamingStart])

  const markStreamingEnd = useCallback((chatId) => {
    if (!chatId) return
    const key = String(chatId)
    acknowledgedLocalChatRunIdsRef.current.delete(key)
    const previous = localStreamingChatIdsRef.current
    if (!previous.has(key)) return
    const next = new Set(previous)
    next.delete(key)
    localStreamingChatIdsRef.current = next
    setLocalStreamingChatIds(next)
  }, [])

  const reconcileLocalChatRuns = useCallback((rows, protectedIds) => {
    const localIds = localStreamingChatIdsRef.current
    for (const chat of rows) {
      const chatId = String(chat.id)
      if (chat.running && localIds.has(chatId)) {
        acknowledgedLocalChatRunIdsRef.current.add(chatId)
      }
    }
    const reconciled = withoutSettledLocalChatRuns(localIds, rows, {
      acknowledgedIds: acknowledgedLocalChatRunIdsRef.current,
      protectedIds,
    })
    if (reconciled === localIds) return reconciled
    for (const chatId of localIds) {
      if (!reconciled.has(chatId)) {
        acknowledgedLocalChatRunIdsRef.current.delete(String(chatId))
      }
    }
    localStreamingChatIdsRef.current = reconciled
    setLocalStreamingChatIds(reconciled)
    return reconciled
  }, [])

  // A ChatView that mounts after a signal treats it as already handled, so a
  // drawer-warmed first-page read begun before the signal must not be joined.
  const bumpSignal = useCallback((chatId, eventType) => {
    if (chatId) forgetChatFirstPageRead(chatId)
    setChatRunSignals(previous => (
      bumpChatRunSignal(previous, chatId, eventType)
    ))
  }, [])
  const markChatRunActivity = useCallback((chatId) => {
    bumpSignal(chatId, 'chat_run_started')
  }, [bumpSignal])
  const markChatRunReconcile = useCallback((chatId) => {
    bumpSignal(chatId, 'chat_run_reconcile')
  }, [bumpSignal])
  const markChatRunFinished = useCallback((chatId) => {
    bumpSignal(chatId, 'chat_run_finished')
  }, [bumpSignal])
  const chatRunSignalFor = useCallback(
    chatId => chatRunSignal(chatRunSignals, chatId),
    [chatRunSignals],
  )

  return {
    chatRunSignalFor,
    markChatRunActivity,
    markChatRunFinished,
    markChatRunReconcile,
    markStreamingAcknowledged,
    markStreamingEnd,
    markStreamingStart,
    reconcileLocalChatRuns,
    streamingChatIds,
    streamingChatIdsRef,
  }
}
