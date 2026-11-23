import { useEffect, useRef } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'summary',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

let bodyScrollLockCount = 0
let bodyOverflowBeforeLock = ''
const dialogStack = []

export function dialogFocusableElements(container) {
  return [...container.querySelectorAll(FOCUSABLE)]
    .filter(element => !element.hidden && element.getClientRects().length > 0)
}

/**
 * Find the branches that must be inert for a dialog with a local modality
 * boundary. The boundary itself stays live so sibling panes outside it are
 * never reached by the global modal contract.
 */
export function dialogSiblingElements(container, boundary = null) {
  const siblings = []
  const seen = new Set()
  const body = typeof document !== 'undefined' ? document.body : null
  let branch = container
  while (branch?.parentElement) {
    const parent = branch.parentElement
    for (const element of parent.children) {
      if (element !== branch && !seen.has(element)) {
        seen.add(element)
        siblings.push(element)
      }
    }
    if (parent === boundary || (body && parent === body)) break
    branch = parent
  }
  return siblings
}

// Nested dialogs can both make the same element inert. Each element is counted and gets its original
// state back only when the last dialog holding it lets go, whichever closes first.
const inertHolds = new WeakMap()

export function holdInert(element) {
  const hold = inertHolds.get(element) || { count: 0, original: element.inert }
  hold.count += 1
  inertHolds.set(element, hold)
  element.inert = true
}

export function releaseInert(element) {
  const hold = inertHolds.get(element)
  if (!hold) return
  hold.count -= 1
  if (hold.count > 0) return
  inertHolds.delete(element)
  element.inert = hold.original
}

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

// A dialog nested inside the one holding the target still comes first.
function keyOwner(target) {
  const top = dialogStack.at(-1)
  const holder = target && dialogStack.findLast(entry => entry.container.contains(target))
  return holder && !holder.container.contains(top.container) ? holder : top
}

// A normal modal blocks every branch up to body; a local one only the branches
// inside its boundary; a non-modal dialog without a boundary blocks nothing.
function blockedElements(container, modal, boundaryRef) {
  if (modal) return dialogSiblingElements(container)
  const boundary = boundaryRef?.current
  return boundary ? dialogSiblingElements(container, boundary) : []
}

/** Focus trap, Escape handling, restoration, and sibling inerting for dialogs. */
export default function useDialogFocus({
  open = true,
  containerRef,
  initialFocusRef,
  restoreFocusRef,
  shouldRestoreFocus,
  onClose,
  closeOnEscape = true,
  // Optional: return false to let an Escape press through without closing (for example while typing).
  shouldCloseOnEscape,
  modal = true,
  lockScroll = modal,
  // A local modal can block only its owning surface while leaving sibling
  // panes interactive. It deliberately does not trap focus or claim a
  // document-wide aria-modal barrier.
  inertBoundaryRef,
}) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const closeOnEscapeRef = useRef(closeOnEscape)
  closeOnEscapeRef.current = closeOnEscape
  const shouldCloseOnEscapeRef = useRef(shouldCloseOnEscape)
  shouldCloseOnEscapeRef.current = shouldCloseOnEscape
  const inertBoundaryRefRef = useRef(inertBoundaryRef)
  inertBoundaryRefRef.current = inertBoundaryRef
  // What the open dialog currently blocks: { container, boundaryRef, elements }.
  const blockedRef = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const container = containerRef.current
    if (!container) return undefined
    let active = true
    const stackEntry = { container }
    dialogStack.push(stackEntry)
    const previouslyFocused = document.activeElement
    const explicitRestoreTarget = restoreFocusRef?.current
    if (lockScroll) lockBodyScroll()

    // Modal dialogs are rendered in place rather than through a body portal.
    // A normal modal inerts sibling branches all the way to body. A local
    // modal stops at its explicit boundary, so only the owning surface is
    // blocked and sibling workspace panes remain interactive.
    const boundaryRef = inertBoundaryRefRef.current
    const blocked = { container, boundaryRef, elements: blockedElements(container, modal, boundaryRef) }
    blocked.elements.forEach(holdInert)
    blockedRef.current = blocked

    const focusInitial = () => {
      if (
        !active
        || !container.isConnected
        || dialogStack.at(-1) !== stackEntry
      ) return
      const target = initialFocusRef?.current
        || dialogFocusableElements(container)[0]
        || container
      target?.focus?.({ preventScroll: true })
    }
    queueMicrotask(focusInitial)

    function onKeyDown(event) {
      // One dialog owns each Escape and Tab: the newest one holding the key's
      // target, else the newest open one. Without this gate, nested/sibling
      // dialogs can both close or both redirect focus from one keypress. Going
      // by the target lets a dialog in one workspace pane keep its keys while
      // a newer, non-blocking dialog (a docked helper preview) is open in
      // another.
      if (keyOwner(event.target) !== stackEntry) return
      const eventIsInsideDialog = container.contains(event.target)
      if (
        event.key === 'Escape'
        && closeOnEscapeRef.current
        && (modal || eventIsInsideDialog)
        && shouldCloseOnEscapeRef.current?.(event) !== false
      ) {
        event.preventDefault()
        onCloseRef.current?.()
        return
      }
      if (event.key !== 'Tab' || !modal) return
      const focusable = dialogFocusableElements(container)
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
      const stackIndex = dialogStack.lastIndexOf(stackEntry)
      if (stackIndex !== -1) dialogStack.splice(stackIndex, 1)
      blocked.elements.forEach(releaseInert)
      if (blockedRef.current === blocked) blockedRef.current = null
      if (lockScroll) unlockBodyScroll()
      if (shouldRestoreFocus?.() === false) return
      if (explicitRestoreTarget) {
        explicitRestoreTarget.focus?.({ preventScroll: true })
      } else {
        previouslyFocused?.focus?.({ preventScroll: true })
      }
    }
  }, [open, containerRef, initialFocusRef, restoreFocusRef, shouldRestoreFocus, lockScroll, modal])

  // A local dialog's blocked area may change while it stays open: a helper
  // preview docks beside a wide chat and covers a narrow one. Swapping the
  // boundary only re-inerts; it never redoes focus capture and restore. A
  // branch holding a dialog opened since (a chat summary over the docked
  // preview) stays live: blocking it would freeze that dialog under this one.
  // Focus that the new block would strand moves into the dialog.
  useEffect(() => {
    const blocked = blockedRef.current
    if (!blocked || blocked.boundaryRef === inertBoundaryRef) return
    const { container } = blocked
    const newer = dialogStack.slice(dialogStack.findIndex(entry => entry.container === container) + 1)
    const next = blockedElements(container, modal, inertBoundaryRef)
      .filter(element => !newer.some(entry => element.contains(entry.container)))
    const active = document.activeElement
    const stranded = !!active && !container.contains(active) && next.some(element => element.contains(active))
    next.forEach(holdInert)
    blocked.elements.forEach(releaseInert)
    Object.assign(blocked, { boundaryRef: inertBoundaryRef, elements: next })
    if (stranded && dialogStack.at(-1)?.container === container) {
      const target = initialFocusRef?.current || dialogFocusableElements(container)[0] || container
      target?.focus?.({ preventScroll: true })
    }
  }, [modal, inertBoundaryRef, initialFocusRef])
}
