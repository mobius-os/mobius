import { editorText, selectEditorRange } from './textEditor.js'

export function shouldApplyComposerFocusRequest({
  focusRequest,
  chatId,
  embedded = false,
} = {}) {
  if (focusRequest?.focus !== true) return false
  if (embedded) return false
  if (focusRequest.chatId == null || chatId == null) return false
  return String(focusRequest.chatId) === String(chatId)
}

export function placeCaretAtTextEnd(el) {
  return selectEditorRange(el, editorText(el).length)
}

export function focusComposerElement(el) {
  if (!el || typeof el.focus !== 'function') return false
  try {
    el.focus({ preventScroll: true })
  } catch {
    el.focus()
  }
  placeCaretAtTextEnd(el)
  return true
}
