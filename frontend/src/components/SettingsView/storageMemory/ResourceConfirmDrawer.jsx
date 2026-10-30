/* Animated confirmation shared by cleanup and Stop, including focus and Escape. */
import { useEffect, useRef } from 'react'

export default function ResourceConfirmDrawer({ id, open, busy, initialFocusRef, restoreFocusRef, onCancel, children }) {
  const wasOpen = useRef(false)
  useEffect(() => {
    if (open) initialFocusRef.current?.focus({ preventScroll: true })
    else if (wasOpen.current) restoreFocusRef.current?.focus({ preventScroll: true })
    wasOpen.current = open
  }, [open, initialFocusRef, restoreFocusRef])

  function onKeyDown(event) {
    if (event.key !== 'Escape' || !open) return
    event.stopPropagation()
    if (busy) return
    event.preventDefault()
    onCancel()
  }

  return (
    <div id={id} className="storage-memory__drawer" data-open={open} inert={!open} onKeyDown={onKeyDown}>
      <div className="storage-memory__drawer-inner">{children}</div>
    </div>
  )
}
