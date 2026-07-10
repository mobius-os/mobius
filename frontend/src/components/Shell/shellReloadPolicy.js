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

export function hasActiveChatTurn({ activeView, activeChatId, streamingChatIds }) {
  if (activeView !== 'chat' || !activeChatId || !streamingChatIds) return false
  if (typeof streamingChatIds.has === 'function') return streamingChatIds.has(activeChatId)
  if (Array.isArray(streamingChatIds)) return streamingChatIds.includes(activeChatId)
  return false
}

export function shouldDeferShellReload({
  activeElement,
  activeView,
  activeChatId,
  streamingChatIds,
  lastUserInteractionAt = 0,
  now = Date.now(),
  visibilityState = 'visible',
} = {}) {
  if (visibilityState === 'hidden') return false
  if (isTextEditingElement(activeElement)) return true
  if (hasActiveChatTurn({ activeView, activeChatId, streamingChatIds })) return true
  if (lastUserInteractionAt && now - lastUserInteractionAt < RECENT_SHELL_INTERACTION_MS) return true
  return false
}
