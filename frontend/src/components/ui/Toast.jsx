import { useEffect, useRef } from 'react'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import './Toast.css'

/**
 * Toast notification primitive.
 *
 * Props:
 *   message   — string to display, or null/undefined to hide
 *   variant   — 'info' (default) | 'error'
 *   duration  — auto-dismiss timeout in ms (default 4000); 0 = no auto-dismiss
 *   onDismiss — called when the toast should disappear (timeout, explicit
 *               dismiss, or undo action if provided)
 *   action    — optional { label: string, onAction: fn } for an inline button
 *               (e.g. "Undo"); clicking it calls onAction then onDismiss.
 *
 * CONTRACT:
 *   - role="status" aria-live="polite" so screen-readers announce the message
 *     without interrupting the user's current task.
 *   - The timer is cleared on unmount (React StrictMode double-invoke safe)
 *     and whenever `message` changes (replacement timer, no stale dismiss).
 *   - Only one Toast should be mounted at a time — Shell.jsx controls this
 *     by keeping a single toast state slot.
 */
export default function Toast({ message, variant = 'info', duration = 4000, onDismiss, action }) {
  const timerRef = useRef(null)

  useEffect(() => {
    // Clear any previous timer first so replacement messages don't
    // inherit a stale timeout that fires immediately.
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (message && duration > 0 && onDismiss) {
      timerRef.current = setTimeout(onDismiss, duration)
    }
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
  }, [message, duration, onDismiss])

  if (!message) return null

  function handleAction() {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    action?.onAction?.()
    onDismiss?.()
  }

  function handleDismiss() {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    onDismiss?.()
  }

  return (
    <div
      className={`toast toast--${variant}`}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <span className="toast__message">{message}</span>
      {action && (
        <button className="toast__action" type="button" onClick={handleAction}>
          {action.label}
        </button>
      )}
      {onDismiss && (
        <button
          className="toast__dismiss"
          type="button"
          aria-label="Dismiss notification"
          onClick={handleDismiss}
        >
          <X size={16} aria-hidden="true" />
        </button>
      )}
    </div>
  )
}
