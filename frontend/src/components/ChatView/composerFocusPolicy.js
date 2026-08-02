export function shouldApplyComposerFocusRequest({
  focusRequest,
  chatId,
  embedded = false,
  isTouchPrimary = false,
} = {}) {
  if (!focusRequest) return false
  if (embedded) return false
  // A touch device may focus only for an explicit composer-focus request.
  // Draft-only handoffs and ordinary navigation must not summon its keyboard.
  if (isTouchPrimary && focusRequest.focus !== true) return false
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
