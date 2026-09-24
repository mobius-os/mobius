import { useEffect, useRef, useState } from 'react'
function emptyState(chatId, status = 'loading') {
  return {
    chatId,
    status,
    layers: { description: '', summary: '', history: [] },
    nextRevision: 0,
    hasMore: false,
    error: '',
  }
}

export function useChatSummaryContinuity(chatId, readPage) {
  const [storedState, setStoredState] = useState(() => emptyState(chatId))
  const [loadingState, setLoadingState] = useState({ chatId, value: false })
  const loadingOlderRef = useRef(false)
  const olderControllerRef = useRef(null)
  const currentChatRef = useRef(chatId)
  currentChatRef.current = chatId
  const state = storedState.chatId === chatId ? storedState : emptyState(chatId)
  const loadingOlder = loadingState.chatId === chatId && loadingState.value

  useEffect(() => {
    const controller = new AbortController()
    async function loadFirstPage() {
      try {
        const data = await readPage(chatId, 0, controller.signal)
        if (controller.signal.aborted) return
        setLoadingState({ chatId, value: false })
        setStoredState({
          chatId,
          status: 'ready',
          layers: {
            description: data.title || '',
            summary: data.summary || '',
            history: data.entries || [],
          },
          nextRevision: data.next_after_revision || 0,
          hasMore: Boolean(data.has_more),
          error: '',
        })
      } catch (error) {
        if (controller.signal.aborted) return
        setLoadingState({ chatId, value: false })
        setStoredState({ ...emptyState(chatId, 'error'), error: error?.message || 'Could not load the chat summary.' })
      }
    }
    loadFirstPage()
    return () => {
      controller.abort()
      olderControllerRef.current?.abort()
      olderControllerRef.current = null
      loadingOlderRef.current = false
    }
  }, [chatId, readPage])

  async function loadMore() {
    if (loadingOlderRef.current || !state.hasMore) return
    loadingOlderRef.current = true
    setLoadingState({ chatId, value: true })
    const controller = new AbortController()
    olderControllerRef.current = controller
    try {
      const page = await readPage(chatId, state.nextRevision, controller.signal)
      if (controller.signal.aborted || currentChatRef.current !== chatId) return
      setStoredState(current => {
        if (current.chatId !== chatId) return current
        return {
          ...current,
          layers: { ...current.layers, history: [...current.layers.history, ...(page.entries || [])] },
          nextRevision: page.next_after_revision || current.nextRevision,
          hasMore: Boolean(page.has_more),
          error: '',
        }
      })
    } catch (error) {
      if (!controller.signal.aborted && currentChatRef.current === chatId) {
        setStoredState(current => current.chatId === chatId
          ? { ...current, error: error?.message || 'Could not load more checkpoints.' }
          : current)
      }
    } finally {
      if (!controller.signal.aborted && currentChatRef.current === chatId) {
        loadingOlderRef.current = false
        olderControllerRef.current = null
        setLoadingState({ chatId, value: false })
      }
    }
  }

  return { state, loadMore, loadingOlder }
}
