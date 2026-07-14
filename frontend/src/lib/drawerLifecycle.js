// Drawer lifecycle helpers keep imperative swipe styling subordinate to the
// shell's authoritative open/closed state.

// Slightly longer than Drawer.css's 250ms transform transition. This is only
// a fallback for reduced-motion / interrupted transitions where transitionend
// does not fire; the normal path releases on the real transform event.
export const DRAWER_CLOSE_FALLBACK_MS = 350

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
