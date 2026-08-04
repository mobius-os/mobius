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
import {
  SHELL_SHORTCUTS,
  shortcutMatches,
} from '../../lib/keyboardShortcuts.js'
import NotificationBell from './NotificationBell.jsx'
import useNotificationCenter from './useNotificationCenter.js'

const NotificationCenter = forwardRef(function NotificationCenter(
  { onOpenTarget },
  eventActionsRef,
) {
  const queryClient = useQueryClient()
  const searchButtonRef = useRef(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const {
    state: { open, unreadCount },
    actions: { toggle, close, clearAll, reconcile, onCreated },
    meta: { rootRef, bellRef },
  } = useNotificationCenter(queryClient)

  // System events stay a narrow nudge from Shell while all interactive state
  // remains local to this component.
  useImperativeHandle(eventActionsRef, () => ({
    reconcile,
    onCreated,
  }), [onCreated, reconcile])

  const openTarget = useCallback((target) => {
    close()
    setSearchOpen(false)
    onOpenTarget?.(target)
  }, [close, onOpenTarget])

  const openSearch = useCallback(() => {
    close()
    setSearchOpen(true)
  }, [close])

  const toggleSearch = useCallback(() => {
    close()
    setSearchOpen(value => !value)
  }, [close])

  const toggleNotifications = useCallback(() => {
    setSearchOpen(false)
    toggle()
  }, [toggle])

  useEffect(() => {
    const onKeyDown = (event) => {
      if (!shortcutMatches(event, SHELL_SHORTCUTS.openSearch)) return
      event.preventDefault()
      openSearch()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [openSearch])

  return (
    <div ref={rootRef} className="notification-center">
      <GlobalSearchButton
        buttonRef={searchButtonRef}
        active={searchOpen}
        onClick={toggleSearch}
      />
      <NotificationBell
        buttonRef={bellRef}
        unreadCount={unreadCount}
        active={open}
        onClick={toggleNotifications}
      />
      {open && (
        <NotificationsView
          active
          onOpenTarget={openTarget}
          onClearAll={clearAll}
        />
      )}
      {searchOpen && (
        <GlobalSearch
          onClose={() => setSearchOpen(false)}
          onOpenTarget={openTarget}
        />
      )}
    </div>
  )
})

export default NotificationCenter
