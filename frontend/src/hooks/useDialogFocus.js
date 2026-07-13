import { useEffect, useRef } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

let bodyScrollLockCount = 0
let bodyOverflowBeforeLock = ''

function lockBodyScroll() {
  if (bodyScrollLockCount === 0) {
    bodyOverflowBeforeLock = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  }
  bodyScrollLockCount += 1
}

function unlockBodyScroll() {
  bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1)
  if (bodyScrollLockCount === 0) {
    document.body.style.overflow = bodyOverflowBeforeLock
  }
}

/** Focus trap, Escape handling, restoration, and sibling inerting for dialogs. */
export default function useDialogFocus({
  open = true,
  containerRef,
  initialFocusRef,
  onClose,
  closeOnEscape = true,
  lockScroll = true,
}) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const closeOnEscapeRef = useRef(closeOnEscape)
  closeOnEscapeRef.current = closeOnEscape

  useEffect(() => {
    if (!open) return undefined
    const container = containerRef.current
    if (!container) return undefined
    let active = true
    const previouslyFocused = document.activeElement
    if (lockScroll) lockBodyScroll()

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
      if (!active || !container.isConnected) return
      const target = initialFocusRef?.current
        || container.querySelector(FOCUSABLE)
        || container
      target?.focus?.({ preventScroll: true })
    }
    queueMicrotask(focusInitial)

    function onKeyDown(event) {
      if (event.key === 'Escape' && closeOnEscapeRef.current) {
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
      active = false
      document.removeEventListener('keydown', onKeyDown, true)
      siblings.forEach(({ element, inert }) => { element.inert = inert })
      if (lockScroll) unlockBodyScroll()
      previouslyFocused?.focus?.({ preventScroll: true })
    }
  }, [open, containerRef, initialFocusRef, lockScroll])
}
