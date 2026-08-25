/* NotificationCenter owns the header's notification and search overlays so
   their local interaction never asks the workspace shell to render again. */
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react'
import { useQueryClient } from '@tanstack/react-query'
import GlobalSearch, { GlobalSearchButton } from '../GlobalSearch/GlobalSearch.jsx'
import NotificationsView from '../NotificationsView/NotificationsView.jsx'
import NotificationBell from './NotificationBell.jsx'
import useNotificationCenter from './useNotificationCenter.js'

const NotificationCenter = forwardRef(function NotificationCenter(
  {
    commands,
    onOpenTarget,
    onRunCommand,
    updateAvailable = false,
    onUpdateNow,
  },
  eventActionsRef,
) {
  const queryClient = useQueryClient()
  const searchButtonRef = useRef(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const [updateNoticeSeen, setUpdateNoticeSeen] = useState(false)
  const {
    state: { open, unreadCount },
    actions: { toggle, close, clearAll, reconcile, onCreated },
    meta: { rootRef, bellRef },
  } = useNotificationCenter(queryClient)
  const updateNoticeActive = updateAvailable && typeof onUpdateNow === 'function'
  const visibleUnreadCount = unreadCount + (
    updateNoticeActive && !updateNoticeSeen ? 1 : 0
  )

  useEffect(() => {
    if (!updateNoticeActive) {
      setUpdateNoticeSeen(false)
    } else if (open) {
      setUpdateNoticeSeen(true)
    }
  }, [open, updateNoticeActive])

  const openTarget = useCallback((target) => {
    close()
    setSearchOpen(false)
    onOpenTarget?.(target)
  }, [close, onOpenTarget])

  const openSearch = useCallback(() => {
    close()
    setSearchOpen(true)
  }, [close])

  // System events and the shell command dispatcher stay narrow nudges while
  // all interactive overlay state remains local to this component.
  useImperativeHandle(eventActionsRef, () => ({
    reconcile,
    onCreated,
    openSearch,
  }), [onCreated, openSearch, reconcile])

  const toggleSearch = useCallback(() => {
    close()
    setSearchOpen(value => !value)
  }, [close])

  const toggleNotifications = useCallback(() => {
    setSearchOpen(false)
    if (updateNoticeActive) setUpdateNoticeSeen(true)
    toggle()
  }, [toggle, updateNoticeActive])

  const applyUpdate = useCallback(() => {
    setUpdateNoticeSeen(true)
    close()
    onUpdateNow?.()
  }, [close, onUpdateNow])

  const deferUpdate = useCallback(() => {
    setUpdateNoticeSeen(true)
    close()
    bellRef.current?.focus()
  }, [bellRef, close])

  return (
    <div ref={rootRef} className="notification-center">
      <GlobalSearchButton
        buttonRef={searchButtonRef}
        active={searchOpen}
        onClick={toggleSearch}
      />
      <NotificationBell
        buttonRef={bellRef}
        unreadCount={visibleUnreadCount}
        active={open}
        onClick={toggleNotifications}
      />
      {open && (
        <NotificationsView
          active
          onOpenTarget={openTarget}
          onClearAll={clearAll}
          updateAvailable={updateNoticeActive}
          onUpdateNow={applyUpdate}
          onUpdateLater={deferUpdate}
        />
      )}
      {searchOpen && (
        <GlobalSearch
          commands={commands}
          onClose={() => setSearchOpen(false)}
          onOpenTarget={openTarget}
          onRunCommand={onRunCommand}
        />
      )}
    </div>
  )
})

export default NotificationCenter
