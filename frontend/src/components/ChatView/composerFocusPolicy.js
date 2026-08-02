export function shouldApplyComposerFocusRequest({
  focusRequest,
  chatId,
  embedded = false,
  isTouchPrimary = false,
} = {}) {
  if (!focusRequest) return false
  if (embedded) return false
  if (isTouchPrimary) return false
  if (focusRequest.chatId == null || chatId == null) return false
  return String(focusRequest.chatId) === String(chatId)
}

export function focusComposerElement(el) {
  if (!el || typeof el.focus !== 'function') return false
  try {
    el.focus({ preventScroll: true })
  } catch {
    el.focus()
  }
  return true
}

/**
 * Keep a pointer interaction inside the composer from moving focus away from
 * its textarea. This deliberately applies to touch as well as mouse/pen:
 * mobile browsers perform the focus default between pointerdown and click, so
 * attempting to refocus from the later click can be too late to stop the soft
 * keyboard collapsing.
 */
export function preserveComposerInputFocus(event) {
  if (!event || typeof event.preventDefault !== 'function') return false
  event.preventDefault()
  return true
}
