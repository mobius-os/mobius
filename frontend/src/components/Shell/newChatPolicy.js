function normalizedId(value) {
  return value == null ? null : String(value)
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

/** Publish the narrow POST /chats response into the list cache immediately.
 * The authoritative list still revalidates in the background; this row exists
 * so navigation and cross-tab guards do not wait for a second request. */
export function addCreatedChatToList(
  current,
  created,
  now = new Date().toISOString(),
) {
  if (!created?.id) throw new Error('Created chat is missing an id')

  const existing = Array.isArray(current)
    ? current.filter(chat => String(chat.id) !== String(created.id))
    : []
  const firstUnpinned = existing.findIndex(chat => !chat.pinned_at)
  const insertAt = firstUnpinned === -1 ? existing.length : firstUnpinned
  const row = {
    id: created.id,
    title: created.title || 'New chat',
    updated_at: created.updated_at || now,
    activity_at: created.activity_at || now,
    pinned_at: null,
    has_messages: Array.isArray(created.messages) && created.messages.length > 0,
    created_by_app_id: created.created_by_app_id ?? null,
    run_status: null,
    running: false,
  }

  return [
    ...existing.slice(0, insertAt),
    row,
    ...existing.slice(insertAt),
  ]
}
