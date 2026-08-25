/* NotificationCenter owns the header's notification and search overlays so
   their local interaction never asks the workspace shell to render again. */
import {
  forwardRef,
  useCallback,
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
  { commands, onOpenTarget, onRunCommand },
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
    toggle()
  }, [toggle])

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
