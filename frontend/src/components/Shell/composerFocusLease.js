import { focusComposerElement } from '../ChatView/composerFocusPolicy.js'

const TOUCH_PRIMARY_QUERY = '(hover: none) and (pointer: coarse)'

/**
 * Keep the software keyboard open across an async New-chat allocation. The
 * lease is focused synchronously inside the owner's tap; the real composer
 * takes that focus once its chat-bound surface mounts.
 */
export function beginTouchComposerFocusLease(el, {
  matchMediaImpl = globalThis.matchMedia,
  activeElement = globalThis.document?.activeElement,
} = {}) {
  if (!el || activeElement === el || typeof matchMediaImpl !== 'function') return false
  if (matchMediaImpl(TOUCH_PRIMARY_QUERY)?.matches !== true) return false
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
