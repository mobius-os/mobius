function normalizedId(value) {
  return value == null ? null : String(value)
}

/**
 * True only for the edge into the first-class empty single-screen surface.
 *
 * Keeping this at the workspace-dispatch boundary means every reducer action
 * that clears the slot (close, prune, restore, mode flip, or a future action)
 * inherits the New Chat policy without adding another call-site repair. The
 * edge check is important: actions while the landing is already visible must
 * not manufacture new request tokens.
 */
export function enteredEmptySingleScreen(previous, next, splitsEnabled = true) {
  const previousSingle = !splitsEnabled || previous?.viewMode === 'single'
  const nextSingle = !splitsEnabled || next?.viewMode === 'single'
  return nextSingle
    && next?.singleScreen == null
    && (!previousSingle || previous?.singleScreen != null)
}

/**
 * Return the only client-side chat that is safe to consider for reuse.
 *
 * An off-screen empty row may belong to another browser that has just sent a
 * message while this tab's list cache is stale. Reusing it makes an explicit
 * "New chat" tap open that running conversation. The current chat is
 * different: keeping an already-open blank open is the intended no-op that
 * prevents repeated taps from manufacturing duplicate blanks. The caller
 * still verifies this candidate against the detail endpoint while online.
 */
export function currentReusableEmptyChat(chats, {
  activeChatId,
  draft = false,
  exclude = null,
  forceNew = false,
  recoveredChatIds = new Set(),
  streamingChatIds = new Set(),
} = {}) {
  if (forceNew || draft || activeChatId == null) return null

  const activeId = normalizedId(activeChatId)
  const excludedId = normalizedId(exclude)
  const recoveredIds = new Set([...recoveredChatIds].map(normalizedId))
  const streamingIds = new Set([...streamingChatIds].map(normalizedId))

  const chat = (chats || []).find(row => normalizedId(row?.id) === activeId)
  if (!chat || chat.has_messages) return null
  if (excludedId != null && activeId === excludedId) return null
  if (recoveredIds.has(activeId) || streamingIds.has(activeId)) return null
  if (chat.running || chat.run_status === 'running') return null
  return chat
}

/**
 * Validate the fresh detail response before keeping an active blank open.
 * Fail closed when the response is partial or unfamiliar: creating a fresh
 * row is preferable to navigating into a conversation that has started in
 * another browser.
 */
export function detailIsUntouchedEmptyChat(detail) {
  if (!detail || typeof detail !== 'object') return false
  if (!Number.isInteger(detail.total) || detail.total !== 0) return false
  if (!Array.isArray(detail.messages) || detail.messages.length !== 0) return false
  if (!Array.isArray(detail.pending_messages) || detail.pending_messages.length !== 0) return false
  if (detail.running) return false
  if (detail.pending_question_id != null) return false
  if (detail.session_id != null) return false
  return true
}

/** Convert a complete create response into ChatView's persisted cache shape.
 * Older/local backends that return only the historical summary fail closed and
 * keep the existing detail fetch path. */
export function createdChatDetailCache(created) {
  const detail = created?.detail
  if (!detailIsUntouchedEmptyChat(detail)) return null
  if (typeof detail.provider !== 'string') return null
  if (!detail.effective_agent_settings
      || typeof detail.effective_agent_settings !== 'object') return null
  if (typeof detail.has_assistant_turns !== 'boolean') return null

  return {
    messages: detail.messages,
    pending_messages: detail.pending_messages,
    pending_question_id: detail.pending_question_id,
    total: detail.total,
    offset: detail.offset,
    running: detail.running,
    chatInfo: {
      provider: detail.provider,
      created_by_app_id: detail.created_by_app_id ?? null,
      agent_settings_json: detail.agent_settings_json || null,
      effective: detail.effective_agent_settings,
      has_assistant_turns: detail.has_assistant_turns,
      auto_resume_on_limit: !!detail.auto_resume_on_limit,
    },
  }
}

/** Publish the narrow POST /chats response into the list cache immediately.
 * The authoritative list still revalidates in the background; this row exists
 * so navigation and cross-tab guards do not wait for a second request. */
export function addCreatedChatToList(
  current,
  created,
) {
  if (!created?.id) throw new Error('Created chat is missing an id')

  const existing = Array.isArray(current)
    ? current.filter(chat => String(chat.id) !== String(created.id))
    : []
  const firstUnpinned = existing.findIndex(chat => !chat.pinned_at)
  const insertAt = firstUnpinned === -1 ? existing.length : firstUnpinned
  const { messages, detail, ...serverRow } = created
  const row = {
    ...serverRow,
    has_messages: typeof created.has_messages === 'boolean'
      ? created.has_messages
      : Array.isArray(messages) && messages.length > 0,
  }

  return [
    ...existing.slice(0, insertAt),
    row,
    ...existing.slice(insertAt),
  ]
}
