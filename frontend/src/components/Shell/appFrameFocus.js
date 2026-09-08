export function releaseFocusFromHiddenAppFrame({
  activeElement = globalThis.document?.activeElement,
  focusTarget,
} = {}) {
  if (activeElement?.tagName !== 'IFRAME') return false
  const owner = activeElement.closest?.('[data-app-frame-owner]')
  if (owner?.getAttribute?.('aria-hidden') !== 'true') return false
  if (typeof focusTarget?.focus !== 'function') return false
  focusTarget.focus({ preventScroll: true })
  return true
}
