/* Session feedback joins the notification feed without timers or remote pushes. */
import { useCallback, useEffect, useRef, useState } from 'react'

// Notices sharing a key are one evolving notice: the newest replaces the
// earlier row, so a retry's outcome or a later archive takes its place.
function addSessionNotice(rows, notice) {
  return [notice, ...rows.filter(row => !notice.noticeKey || row.noticeKey !== notice.noticeKey)]
}

function sessionNoticeCounts(rows) {
  return {
    unreadCount: rows.filter(row => !row.read_at).length,
    newCount: rows.filter(row => !row.seen_at).length,
  }
}

export default function useSessionNotices(open = false) {
  const [rows, setRows] = useState([])
  const [announcement, setAnnouncement] = useState(null)
  const sequenceRef = useRef(0)
  const openRef = useRef(open)
  openRef.current = open
  const workingRef = useRef(new Set())
  const rowsRef = useRef(rows)
  rowsRef.current = rows

  const addNotice = useCallback((message, { variant = 'info', action, noticeKey } = {}) => {
    const timestamp = new Date().toISOString()
    sequenceRef.current += 1
    const notice = {
      id: `shell-notice-${sequenceRef.current}`,
      source_type: 'system',
      title: message,
      sent_at: timestamp,
      read_at: null,
      seen_at: openRef.current ? timestamp : null,
      variant,
      noticeKey,
      sessionAction: action,
    }
    setRows(current => addSessionNotice(current, notice))
    setAnnouncement({ id: notice.id, title: message })
  }, [])

  useEffect(() => {
    if (!open) return
    const timestamp = new Date().toISOString()
    setRows(current => current.map(row => (
      row.seen_at ? row : { ...row, seen_at: timestamp }
    )))
  }, [open])

  const markRead = useCallback((ids = rowsRef.current.map(row => row.id)) => {
    const selected = new Set(ids)
    const timestamp = new Date().toISOString()
    setRows(current => current.map(row => (
      selected.has(row.id) ? { ...row, read_at: timestamp, seen_at: timestamp } : row
    )))
  }, [])

  const dismiss = useCallback((id) => {
    setRows(current => current.filter(row => row.id !== id))
  }, [])
  const clearAll = useCallback((ids = rowsRef.current.map(row => row.id)) => {
    const selected = new Set(ids)
    setRows(current => current.filter(row => !selected.has(row.id) || row.sessionAction))
  }, [])

  const runAction = useCallback(async (id) => {
    const row = rowsRef.current.find(notice => notice.id === id)
    if (!row?.sessionAction || workingRef.current.has(id)) return
    workingRef.current.add(id)
    setRows(current => current.map(notice => notice.id === id
      ? { ...notice, actionWorking: true, actionError: null } : notice))
    try {
      const completed = await row.sessionAction.onAction()
      if (completed === false) throw new Error('Couldn’t complete this action. Try again.')
      setRows(current => current.map(notice => notice.id === id ? {
        ...notice, sessionAction: null, actionStatus: 'Undone',
        actionWorking: false, read_at: new Date().toISOString(),
      } : notice))
    } catch (error) {
      setRows(current => current.map(notice => notice.id === id ? {
        ...notice, actionWorking: false,
        actionError: error?.message || 'Couldn’t complete this action. Try again.',
      } : notice))
    } finally {
      workingRef.current.delete(id)
    }
  }, [])

  return { rows, announcement, ...sessionNoticeCounts(rows), addNotice, markRead, dismiss, clearAll, runAction }
}
