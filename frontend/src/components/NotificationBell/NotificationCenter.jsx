/* NotificationCenter owns the bell and preview so toggling this small overlay
   never asks the workspace shell to render again. */
import { forwardRef, useCallback, useImperativeHandle } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import NotificationsView from '../NotificationsView/NotificationsView.jsx'
import NotificationBell from './NotificationBell.jsx'
import useNotificationCenter from './useNotificationCenter.js'

const NotificationCenter = forwardRef(function NotificationCenter(
  { onOpenTarget },
  eventActionsRef,
) {
  const queryClient = useQueryClient()
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
    onOpenTarget?.(target)
  }, [close, onOpenTarget])

  return (
    <div ref={rootRef} className="notification-center">
      <NotificationBell
        buttonRef={bellRef}
        unreadCount={unreadCount}
        active={open}
        onClick={toggle}
      />
      {open && (
        <NotificationsView
          active
          onOpenTarget={openTarget}
          onClearAll={clearAll}
        />
      )}
    </div>
  )
})

export default NotificationCenter
