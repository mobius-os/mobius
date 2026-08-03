import { useEffect } from 'react'

/**
 * Dismiss a pointer-transparent context menu without consuming the destination
 * click. Parent-document pointer events cover shell and chat surfaces; focus
 * transfer covers clicks entering an app iframe, whose pointer stream never
 * bubbles into the shell document.
 */
export default function useContextMenuOutsideDismiss({
  open,
  menuRef,
  onDismiss,
}) {
  useEffect(() => {
    if (!open) return undefined
    let active = true

    function dismissFromOutsidePointer(event) {
      if (!menuRef.current?.contains(event.target)) onDismiss()
    }

    function dismissFromFrameFocus() {
      // Browsers disagree about whether activeElement changes before or just
      // after the parent window's blur listener. Check at the microtask edge so
      // the iframe owns focus in either ordering without delaying its click.
      queueMicrotask(() => {
        if (active && document.activeElement?.tagName === 'IFRAME') onDismiss()
      })
    }

    document.addEventListener('pointerdown', dismissFromOutsidePointer, true)
    window.addEventListener('blur', dismissFromFrameFocus, true)
    return () => {
      active = false
      document.removeEventListener('pointerdown', dismissFromOutsidePointer, true)
      window.removeEventListener('blur', dismissFromFrameFocus, true)
    }
  }, [menuRef, onDismiss, open])
}
