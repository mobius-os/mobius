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

export function hasActiveChatTurn({ activeView, activeChatId, streamingChatIds }) {
  // A controlled shell reload must never interrupt the chat the owner is
  // actively watching. Background runs are different: they are server-owned,
  // survive the page reload, and reconnect/catch up afterwards. Treating ANY
  // background run as foreground activity can strand a repaired shell forever
  // when the owner has several agents working at once.
  if (activeView !== 'chat' || activeChatId == null || !streamingChatIds) return false
  const id = String(activeChatId)
  if (typeof streamingChatIds.has === 'function') {
    return streamingChatIds.has(activeChatId) || streamingChatIds.has(id)
  }
  if (Array.isArray(streamingChatIds)) {
    return streamingChatIds.some(chatId => String(chatId) === id)
  }
  return false
}

export function shouldDeferShellReload({
  activeElement,
  activeView,
  activeChatId,
  builderWorkspaceVisible = false,
  streamingChatIds,
  passiveRebuild = false,
  voiceDictationActive = false,
  lastUserInteractionAt = 0,
  now = Date.now(),
  visibilityState = 'visible',
} = {}) {
  if (visibilityState === 'hidden') return false
  // Builder is one live workspace, not merely the focused route. Reloading it
  // at the focused chat's idle boundary tears down every visible chat and app
  // pane together, producing a workspace-wide blank/reload even when the other
  // panes are actively being read or used. Keep all shell generations queued
  // until the page is backgrounded or the owner returns to Standard. This is
  // the multi-pane counterpart of the visible-canvas protection below.
  if (builderWorkspaceVisible) return true
  // Canvas apps may contain games, unsaved work, media, or nested fullscreen
  // documents whose state is opaque to the shell. Never tear down that visible
  // surface for a background shell generation. The existing active-view and
  // visibility effects release the queued reload after the owner leaves the
  // app or backgrounds the page.
  if (activeView === 'canvas') return true
  // Watcher rebuilds are noisy implementation detail: a burst of source saves
  // can publish several generations while the owner is simply reading an idle
  // chat. Keep those generations coalesced until the chat is no longer the
  // visible surface. A deliberate shell_apply_now is not passive and continues
  // through the ordinary apply-on-idle policy below.
  if (passiveRebuild && activeView === 'chat' && activeChatId != null) return true
  if (hasProtectedEditingContent(activeElement)) return true
  if (hasActiveChatTurn({ activeView, activeChatId, streamingChatIds })) return true
  // A reload mid-dictation would drop the in-flight transcript, so hold it
  // while the mic is live.
  if (voiceDictationActive) return true
  if (lastUserInteractionAt && now - lastUserInteractionAt < RECENT_SHELL_INTERACTION_MS) return true
  return false
}
