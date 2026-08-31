/* Connect a transient shell surface to the browser Back stack without owning navigation itself. */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from 'react'

const HistoryDismissContext = createContext(null)

export function HistoryDismissProvider({
  children,
  openHistoryDismiss,
  closeHistoryDismiss,
  unregisterHistoryDismiss,
}) {
  const value = useMemo(() => ({
    open: openHistoryDismiss,
    close: closeHistoryDismiss,
    unregister: unregisterHistoryDismiss,
  }), [closeHistoryDismiss, openHistoryDismiss, unregisterHistoryDismiss])

  return (
    <HistoryDismissContext.Provider value={value}>
      {children}
    </HistoryDismissContext.Provider>
  )
}

/**
 * Raw multi-entry access to the shell's history-dismiss stack for a surface that
 * needs more than one live sentinel at a time.
 */
export function useHistoryDismissControls() {
  return useContext(HistoryDismissContext)
}

/**
 * Returns the paired open/close operations for one conditionally rendered
 * surface. Opening pushes its sentinel before the surface paints; closing
 * consumes that same sentinel through the shell's navigation owner.
 */
export function useHistoryDismiss(onDismiss) {
  const historyDismiss = useContext(HistoryDismissContext)
  const entryIdRef = useRef(null)
  const onDismissRef = useRef(onDismiss)
  onDismissRef.current = onDismiss

  const dismissFromHistory = useCallback(() => {
    entryIdRef.current = null
    onDismissRef.current?.()
  }, [])

  const open = useCallback(() => {
    if (entryIdRef.current || !historyDismiss?.open) return
    entryIdRef.current = historyDismiss.open(dismissFromHistory)
  }, [dismissFromHistory, historyDismiss])

  const close = useCallback(() => {
    const entryId = entryIdRef.current
    if (entryId && historyDismiss?.close?.(entryId)) return
    entryIdRef.current = null
    onDismissRef.current?.()
  }, [historyDismiss])

  useEffect(() => () => {
    const entryId = entryIdRef.current
    if (entryId) historyDismiss?.unregister?.(entryId)
    entryIdRef.current = null
  }, [historyDismiss])

  return { open, close }
}
