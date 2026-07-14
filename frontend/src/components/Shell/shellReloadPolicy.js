export const RECENT_SHELL_INTERACTION_MS = 2000

export function isTextEditingElement(el) {
  if (!el) return false
  const tagName = String(el.tagName || '').toLowerCase()
  if (tagName === 'iframe') return true
  if (tagName === 'textarea' || tagName === 'select') return true
  if (tagName === 'input') {
    const type = String(el.type || '').toLowerCase()
    return !['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'].includes(type)
  }
  if (el.isContentEditable) return true
  if (typeof el.getAttribute === 'function') {
    const role = String(el.getAttribute('role') || '').toLowerCase()
    if (role === 'textbox' || role === 'searchbox' || role === 'combobox') return true
  }
  if (typeof el.closest === 'function') {
    return !!el.closest('textarea,input,select,[contenteditable="true"],[role="textbox"],[role="searchbox"],[role="combobox"]')
  }
  return false
}

// A focused editing surface only blocks an apply-on-idle reload when it holds
// content the reload would DESTROY. The resting state right after the owner
// sends a message is an EMPTY composer that keeps focus on desktop (see
// ChatInputBar: the textarea stays focused after send on non-touch devices) —
// there is nothing to protect. Treating that bare, blinking cursor as "still
// composing" is what stalled the held reload forever and broke the design's
// promise that apply-on-idle "feels live while the owner is watching a build
// session" (§1.3): the shell update the owner just triggered never landed
// while their cursor sat idle in the empty composer. So an EMPTY text editor
// counts as idle.
//
// A focused cross-origin mini-app iframe is opaque — we cannot tell whether the
// user is mid-interaction — so it stays protected (a reload would yank a live
// app). Selects and role-based custom editors whose text we cannot read stay
// protected too, conservatively: only a field we can positively read as empty
// is cleared to apply.
export function hasProtectedEditingContent(el) {
  if (!isTextEditingElement(el)) return false
  const tagName = String(el.tagName || '').toLowerCase()
  if (tagName === 'input' || tagName === 'textarea') {
    return String(el.value ?? '').length > 0
  }
  if (el.isContentEditable) {
    return String(el.textContent ?? '').length > 0
  }
  // iframe / select / role=textbox|searchbox|combobox custom widgets: opaque or
  // non-text. Keep the reload deferred while they hold focus.
  return true
}

export function hasActiveChatTurn({ streamingChatIds }) {
  if (!streamingChatIds) return false
  if (typeof streamingChatIds.size === 'number') return streamingChatIds.size > 0
  if (Array.isArray(streamingChatIds)) return streamingChatIds.length > 0
  return false
}

export function shouldDeferShellReload({
  activeElement,
  activeView,
  activeChatId,
  streamingChatIds,
  voiceDictationActive = false,
  lastUserInteractionAt = 0,
  now = Date.now(),
  visibilityState = 'visible',
} = {}) {
  if (visibilityState === 'hidden') return false
  if (hasProtectedEditingContent(activeElement)) return true
  if (hasActiveChatTurn({ activeView, activeChatId, streamingChatIds })) return true
  // A reload mid-dictation would drop the in-flight transcript, so hold it
  // while the mic is live.
  if (voiceDictationActive) return true
  if (lastUserInteractionAt && now - lastUserInteractionAt < RECENT_SHELL_INTERACTION_MS) return true
  return false
}
