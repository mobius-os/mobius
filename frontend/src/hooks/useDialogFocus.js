import { useEffect, useRef } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** Focus trap, Escape handling, restoration, and sibling inerting for dialogs. */
export default function useDialogFocus({
  open = true,
  containerRef,
  initialFocusRef,
  onClose,
  closeOnEscape = true,
}) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return undefined
    const container = containerRef.current
    if (!container) return undefined
    const previouslyFocused = document.activeElement

    // The dialog is rendered in place rather than through a body portal. Inert
    // sibling branches all the way to body so shell controls behind the modal
    // cannot remain keyboard- or assistive-technology reachable.
    const siblings = []
    let branch = container
    while (branch.parentElement) {
      const parent = branch.parentElement
      for (const element of parent.children) {
        if (element !== branch && !siblings.some(entry => entry.element === element)) {
          siblings.push({ element, inert: element.inert })
          element.inert = true
        }
      }
      if (parent === document.body) break
      branch = parent
    }

    const focusInitial = () => {
      const target = initialFocusRef?.current
        || container.querySelector(FOCUSABLE)
        || container
      target?.focus?.({ preventScroll: true })
    }
    queueMicrotask(focusInitial)

    function onKeyDown(event) {
      if (event.key === 'Escape' && closeOnEscape) {
        event.preventDefault()
        onCloseRef.current?.()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = [...container.querySelectorAll(FOCUSABLE)]
        .filter(element => !element.hidden && element.getClientRects().length > 0)
      if (focusable.length === 0) {
        event.preventDefault()
        container.focus?.()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      siblings.forEach(({ element, inert }) => { element.inert = inert })
      previouslyFocused?.focus?.({ preventScroll: true })
    }
  }, [open, closeOnEscape, containerRef, initialFocusRef])
}
