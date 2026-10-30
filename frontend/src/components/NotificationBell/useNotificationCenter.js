import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client.js'
import { notificationQueries } from '../../hooks/queries.js'
import useSessionNotices from './useSessionNotices.js'

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
  const newQuery = notificationQueries.newCount.useQuery()
  const session = useSessionNotices(open)
  const unreadCount = (unreadQuery.data ?? 0) + session.unreadCount
  const newCount = (newQuery.data ?? 0) + session.newCount

  const acknowledgeNew = useCallback(async () => {
    await queryClient.cancelQueries({ queryKey: notificationQueries.newCount.key })
    queryClient.setQueryData(notificationQueries.newCount.key, 0)
    try {
      await api.notifications.seenAll()
    } finally {
      await notificationQueries.newCount.invalidate(queryClient)
    }
  }, [queryClient])

  const markAllRead = useCallback(async () => {
    const noticeIds = session.rows.map(row => row.id)
    await api.notifications.readAll()
    session.markRead(noticeIds)
    await Promise.all([
      notificationQueries.list.invalidate(queryClient),
      notificationQueries.unreadCount.invalidate(queryClient),
      notificationQueries.newCount.invalidate(queryClient),
    ])
  }, [queryClient, session.markRead, session.rows])

  const markRead = useCallback(async (notificationId) => {
    if (session.rows.some(row => row.id === notificationId)) {
      session.markRead([notificationId])
      return
    }
    await api.notifications.read(notificationId)
    await Promise.all([
      notificationQueries.list.invalidate(queryClient),
      notificationQueries.unreadCount.invalidate(queryClient),
      notificationQueries.newCount.invalidate(queryClient),
    ])
  }, [queryClient, session.markRead, session.rows])

  const clearAll = useCallback(async () => {
    // Arrivals during the remote sweep belong to the next history.
    const noticeIds = session.rows.map(row => row.id)
    await api.notifications.clearAll()
    session.clearAll(noticeIds)
    await Promise.all([
      notificationQueries.list.invalidate(queryClient),
      notificationQueries.unreadCount.invalidate(queryClient),
      notificationQueries.newCount.invalidate(queryClient),
    ])
  }, [session.clearAll, session.rows, queryClient])

  const dismiss = useCallback(async (notificationId) => {
    if (session.rows.some(row => row.id === notificationId)) {
      session.dismiss(notificationId)
      return
    }
    await api.notifications.dismiss(notificationId)
    // Refresh in place: resetting discards every page and flashes the loader.
    await notificationQueries.list.invalidate(queryClient)
    notificationQueries.unreadCount.invalidate(queryClient)
    notificationQueries.newCount.invalidate(queryClient)
  }, [session.dismiss, queryClient, session.rows])

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

  const toggle = useCallback(() => {
    if (!openRef.current) void acknowledgeNew().catch(() => {})
    setOpen(value => !value)
  }, [acknowledgeNew])
  const close = useCallback(() => setOpen(false), [])
  const reconcile = useCallback(() => {
    notificationQueries.unreadCount.invalidate(queryClient)
    notificationQueries.newCount.invalidate(queryClient)
    if (openRef.current) notificationQueries.list.invalidate(queryClient)
  }, [queryClient])
  const onCreated = useCallback(() => {
    notificationQueries.unreadCount.invalidate(queryClient)
    notificationQueries.list.invalidate(queryClient)
    notificationQueries.newCount.invalidate(queryClient)
    if (openRef.current) void acknowledgeNew().catch(() => {})
  }, [acknowledgeNew, queryClient])

  return {
    state: { open, unreadCount, newCount, sessionNotices: session.rows, announcement: session.announcement },
    actions: { toggle, close, clearAll, dismiss, markRead, markAllRead, reconcile, onCreated, addNotice: session.addNotice, runNoticeAction: session.runAction },
    meta: { rootRef, bellRef },
  }
}
