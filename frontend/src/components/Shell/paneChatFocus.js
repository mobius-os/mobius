const INTERACTIVE_TARGET = [
  'a',
  'button',
  'input',
  'textarea',
  'select',
  '[contenteditable="true"]',
  '[role="button"]',
  '[role="link"]',
].join(',')

// Pane selection should feel keyboard-forward on a desktop browser without
// summoning a software keyboard on touch devices. Keeping this decision at the
// shell boundary also means every way of selecting a pane follows one rule.
export function supportsDesktopPaneComposerFocus(matchMedia = null) {
  const query = matchMedia || (typeof globalThis.matchMedia === 'function'
    ? globalThis.matchMedia.bind(globalThis)
    : null)
  if (!query) return false
  try {
    return query('(hover: hover) and (pointer: fine)').matches === true
  } catch {
    return false
  }
}

// A click on blank/message space may finish by focusing the newly selected
// pane's composer. Controls keep their native focus and action instead.
export function shouldFocusComposerAfterPanePointer({
  wasFocused,
  pointerType,
  button,
  target,
}) {
  if (wasFocused || pointerType !== 'mouse' || (button != null && button !== 0)) return false
  return !target?.closest?.(INTERACTIVE_TARGET)
}
