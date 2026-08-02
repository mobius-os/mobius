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

const TOUCH_PRIMARY_QUERY = '(hover: none) and (pointer: coarse)'

function isTouchPrimaryFocusEnvironment(
  matchMediaImpl = globalThis.matchMedia,
) {
  if (typeof matchMediaImpl !== 'function') return false
  return matchMediaImpl(TOUCH_PRIMARY_QUERY)?.matches === true
}

/**
 * Keep the software keyboard open across an async New-chat allocation. The
 * lease is focused synchronously inside the owner's tap; the real composer
 * takes that focus once its chat-bound surface mounts.
 */
export function beginTouchComposerFocusLease(el, {
  matchMediaImpl = globalThis.matchMedia,
  activeElement = globalThis.document?.activeElement,
} = {}) {
  if (!el || activeElement === el) return false
  if (!isTouchPrimaryFocusEnvironment(matchMediaImpl)) return false
  el.value = ''
  return focusComposerElement(el)
}

export function releaseComposerFocusLease(el, {
  activeElement = globalThis.document?.activeElement,
} = {}) {
  if (!el) return
  if (activeElement === el && typeof el.blur === 'function') el.blur()
  el.value = ''
}
