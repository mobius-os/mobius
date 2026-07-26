import Bell from 'lucide-react/dist/esm/icons/bell.mjs'
import './NotificationBell.css'

// The header bell — lives in the shell bar's right-side action slot
// (shell__bar-actions), so it renders identically on desktop and mobile by
// construction. A TOGGLE, not just an opener: when the notifications page is
// the active view (`active`), the tap dismisses it through the shell's Back
// path instead of dead-ending on navTo's same-route dedup. The unread badge
// clears when the page marks everything read (seen-on-open model).
export default function NotificationBell({
  unreadCount = 0, active = false, buttonRef, onClick,
}) {
  const count = Number.isFinite(unreadCount) && unreadCount > 0 ? unreadCount : 0
  const label = active
    ? 'Close notifications'
    : (count > 0 ? `Notifications, ${count} unread` : 'Notifications')
  return (
    <button
      ref={buttonRef}
      type="button"
      className={`notification-bell${active ? ' notification-bell--active' : ''}`}
      aria-label={label}
      aria-expanded={active}
      aria-controls="notification-preview"
      title={label}
      onClick={onClick}
    >
      <Bell size={18} aria-hidden="true" />
      {count > 0 && (
        <span className="notification-bell__badge" aria-hidden="true">
          {count > 99 ? '99+' : count}
        </span>
      )}
    </button>
  )
}
