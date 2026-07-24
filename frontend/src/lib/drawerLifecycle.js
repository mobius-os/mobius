// Drawer lifecycle helpers keep imperative swipe styling subordinate to the
// shell's authoritative open/closed state.

// Longer than Drawer.css's 100ms close transition. This is only
// a fallback for reduced-motion / interrupted transitions where transitionend
// does not fire; the normal path releases on the real transform event.
export const DRAWER_CLOSE_FALLBACK_MS = 180
export const DRAWER_SWIPE_THRESHOLD_PX = 10
export const DRAWER_SWIPE_DOMINANCE = 1.15

/** True only when the current displacement is decisively sideways. */
export function isHorizontalDrawerSwipe(dx, dy) {
  return Math.abs(dx) > DRAWER_SWIPE_THRESHOLD_PX
    && Math.abs(dx) > Math.abs(dy) * DRAWER_SWIPE_DOMINANCE
}

/**
 * The drawer's OPEN path must stand down while a workspace drag session is live,
 * exactly like the swipe-CLOSE handlers do. A tab dragged toward the left root
 * edge (to split a left pane) otherwise reads as a left-edge open gesture and
 * pops the drawer over the drop target. `dragActive` is the shared drag flag's
 * current value; anything but a live drag (false/undefined/null) allows the open.
 */
export function drawerOpenBlockedByDrag(dragActive) {
  return dragActive === true
}

/**
 * Only a normally completed custom swipe owns its generated click.
 * A vertical scroll may start with diagonal noise, and touchcancel means the
 * browser took over the gesture; neither path may suppress a later tap.
 */
export function shouldSuppressDrawerSwipeClick({
  sawHorizontalMove,
  cancelled = false,
  dx = 0,
  dy = 0,
}) {
  return !cancelled && !!sawHorizontalMove && isHorizontalDrawerSwipe(dx, dy)
}

/**
 * Distinguish a touch-generated compatibility click from keyboard, assistive,
 * programmatic, or mouse activation. Pointer/key starts normally clear the
 * guard first; this check also fails open for detail=0 accessibility clicks.
 */
export function isGeneratedTouchClick(event) {
  const firesTouchEvents = event?.sourceCapabilities?.firesTouchEvents
  if (firesTouchEvents === true) return true
  if (firesTouchEvents === false) return false
  return Number(event?.detail) > 0
}

/**
 * Remove every DOM mutation made by Drawer.jsx's touch-drag handlers.
 *
 * A browser/app navigation can close the drawer before touchend/touchcancel is
 * delivered. Without an authoritative close cleanup, the stale inline
 * transform wins over `.drawer { transform: translateX(-100%) }`, leaving a
 * visually-open panel whose React `open` prop is false (therefore inert).
 */
export function clearDrawerGestureStyles(element) {
  if (!element) return
  element.classList?.remove?.('drawer--dragging')
  if (element.style) element.style.transform = ''
}
