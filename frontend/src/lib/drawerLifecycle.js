// Drawer lifecycle helpers keep imperative swipe styling subordinate to the
// shell's authoritative open/closed state.

// Extra time beyond the browser's computed transition. This is only a watchdog
// for interrupted transitions where transitionend does not fire; the normal
// path releases on the real transform event. A zero-duration transition needs
// no watchdog (notably prefers-reduced-motion).
export const DRAWER_CLOSE_WATCHDOG_BUFFER_MS = 80
export const DRAWER_SWIPE_THRESHOLD_PX = 10
export const DRAWER_SWIPE_DOMINANCE = 1.15

/**
 * Only the always-visible sidebar follows chat selections made elsewhere.
 *
 * A modal drawer is itself a destination the owner deliberately opens. Moving
 * its list at that moment overrides the last manual scroll position and makes
 * the opening gesture feel like it also scrolled the drawer. The persistent
 * desktop sidebar has no such open gesture, so keeping its active row visible
 * remains useful there.
 */
export function shouldAutoRevealActiveChat({
  open,
  persistent,
  activeView,
  activeChatId,
}) {
  return !!open
    && !!persistent
    && activeView === 'chat'
    && activeChatId != null
}

/** Restore modal-drawer focus only while the drawer still owns it. */
export function shouldRestoreDrawerFocus({
  drawer,
  activeElement,
  body,
  focusHandoffActive = false,
} = {}) {
  return !focusHandoffActive && (
    !activeElement
    || activeElement === body
    || !drawer
    || drawer.contains(activeElement)
  )
}

function cssTimeMs(value) {
  const text = String(value || '').trim()
  const numeric = Number.parseFloat(text)
  if (!Number.isFinite(numeric)) return 0
  return text.endsWith('ms') ? numeric : numeric * 1000
}

/**
 * Return a last-resort close watchdog derived from the panel's computed style.
 *
 * CSS transition lists repeat shorter property/duration/delay lists, so index
 * each value modulo its own list length. `all` also owns the transform. The
 * real transitionend remains authoritative; this only prevents a stranded
 * modal scrim if the browser cancels or omits that event.
 */
export function drawerCloseWatchdogMs(style) {
  const properties = String(style?.transitionProperty || '').split(',').map(v => v.trim())
  const durations = String(style?.transitionDuration || '').split(',').map(cssTimeMs)
  const delays = String(style?.transitionDelay || '').split(',').map(cssTimeMs)
  let longest = 0

  // transition-property defines how many transitions exist. CSS repeats a
  // shorter duration/delay list to that length and truncates surplus values;
  // treating a surplus duration as another `transform` could strand the scrim
  // behind a watchdog the browser itself never scheduled.
  for (let index = 0; index < properties.length; index += 1) {
    const property = properties[index]
    if (property !== 'transform' && property !== 'all') continue
    const duration = durations[index % durations.length]
    const delay = delays[index % delays.length]
    longest = Math.max(longest, Math.max(0, duration + delay))
  }

  return longest > 0
    ? Math.ceil(longest + DRAWER_CLOSE_WATCHDOG_BUFFER_MS)
    : 0
}

/**
 * Resize from the gesture's starting geometry rather than an absolute screen
 * coordinate. `edgeDirection` is 1 for a right-edge handle and -1 for a
 * left-edge handle, so future insets or opposite-side navigation stay correct.
 */
export function drawerWidthFromPointerDelta({
  startWidth,
  startX,
  currentX,
  edgeDirection = 1,
}) {
  const width = Number(startWidth)
  const origin = Number(startX)
  const current = Number(currentX)
  const direction = Number(edgeDirection) < 0 ? -1 : 1
  if (![width, origin, current].every(Number.isFinite)) return width
  return width + ((current - origin) * direction)
}

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
