import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client.js'
import { notificationQueries } from '../../hooks/queries.js'

// Owns the small amount of state behind the shell notification preview. Keeping
// this out of Shell is deliberate: notifications can grow into an app later
// without making navigation, workspace, or pane state aware of that product UI.
export default function useNotificationCenter(queryClient) {
  const [open, setOpen] = useState(false)
  const openRef = useRef(open)
  const rootRef = useRef(null)
  const bellRef = useRef(null)
  openRef.current = open

  const unreadQuery = notificationQueries.unreadCount.useQuery()
  const unreadCount = unreadQuery.data ?? 0

  const markSeen = useCallback(() => (
    api.notifications.readAll()
      .then(() => {
        queryClient.setQueryData(notificationQueries.unreadCount.key, 0)
      })
      .catch(() => { /* Offline is safe: the unread count retries later. */ })
  ), [queryClient])

  const clearAll = useCallback(async () => {
    await api.notifications.clearAll()
    queryClient.setQueryData(notificationQueries.list.key, [])
    queryClient.setQueryData(notificationQueries.unreadCount.key, 0)
  }, [queryClient])

  useEffect(() => {
    if (open) void markSeen()
  }, [open, markSeen])

  useEffect(() => {
    if (!open) return undefined
    const onPointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false)
    }
    const onKeyDown = (event) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      bellRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const toggle = useCallback(() => setOpen(value => !value), [])
  const close = useCallback(() => setOpen(false), [])
  const reconcile = useCallback(() => {
    notificationQueries.unreadCount.invalidate(queryClient)
    if (openRef.current) notificationQueries.list.invalidate(queryClient)
  }, [queryClient])
  const onCreated = useCallback(() => {
    notificationQueries.unreadCount.invalidate(queryClient)
    notificationQueries.list.invalidate(queryClient)
    if (openRef.current) void markSeen()
  }, [markSeen, queryClient])

  return {
    state: { open, unreadCount },
    actions: { toggle, close, clearAll, reconcile, onCreated },
    meta: { rootRef, bellRef },
  }
}
