/**
 * Restore the element that opened a modal drawer only while the closing drawer
 * still owns focus. A destination may deliberately move focus elsewhere before
 * the close effect runs (New chat's phone keyboard lease is one such handoff).
 */
export function shouldRestoreDrawerFocus({
  drawer,
  activeElement,
  body,
} = {}) {
  return !activeElement
    || activeElement === body
    || !drawer
    || drawer.contains(activeElement)
}
