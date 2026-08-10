function normalizedId(value) {
  return value == null ? null : String(value)
}

/**
 * Whether an immediate New Chat surface still owns what the user is seeing.
 *
 * Allocation is allowed to finish only while the route generation, layout
 * world, and drawer-history ownership captured by the tap are unchanged. Once
 * the destination exists, the concrete chat route is the simpler authority:
 * it owns the cover until that ChatView reports a painted frame.
 */
export function newChatPresentationIsCurrent(presentation, {
  navigationEpoch,
  viewMode,
  drawerEntryOpen,
  activeView,
  activeChatId,
} = {}) {
  if (!presentation || presentation.viewMode !== viewMode) return false
  if (presentation.chatId != null) {
    if (activeView !== 'chat') return false
    // The concrete chat route owns the cover once it exists — but navTo bumps
    // the epoch and commits `activeChatId` a render later, so the route is still
    // catching up to the resolved chat for one commit. Treat that in-flight
    // window as current: `activeChatId` already matches, OR no navigation has
    // happened since the cover resolved (epoch unchanged). A real supersede
    // (Back, another chat, an app) bumps the epoch past the resolved value and
    // retires the cover. Without this the cover retires over the OUTGOING chat
    // mid-transition, flashing it between the New chat surface and its
    // destination.
    return normalizedId(activeChatId) === normalizedId(presentation.chatId)
      || presentation.navigationEpoch === navigationEpoch
  }
  return presentation.navigationEpoch === navigationEpoch
    && presentation.drawerEntryOpen === !!drawerEntryOpen
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
export function enteredEmptySingleScreen(previous, next) {
  const previousSingle = previous?.viewMode === 'single'
  const nextSingle = next?.viewMode === 'single'
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
  if (chat.running) return null
  return chat
}

/**
 * Choose the Standard compose surface without applying Builder's add-new rule.
 * Only the currently open untouched blank is eligible. Saved drafts belong to
 * their original chats and must never turn New chat into history navigation.
 */
export function standardNewChatCandidate(chats, draft, {
  activeChatId,
  exclude = null,
  recoveredChatIds = new Set(),
  streamingChatIds = new Set(),
} = {}) {
  const reuseOptions = {
    exclude,
    recoveredChatIds,
    streamingChatIds,
  }
  const active = currentReusableEmptyChat(chats, {
    ...reuseOptions,
    activeChatId,
  })
  if (!active) return null

  const activeDraft = draft
    && normalizedId(draft.chatId) === normalizedId(active.id)
    && (draft.input || draft.attachments?.length)
    ? draft
    : null
  return {
    chatId: active.id,
    source: activeDraft ? 'draft' : 'active',
    draft: activeDraft || null,
  }
}

/** Decide whether candidate provenance is enough without a server round-trip. */
export function newChatCandidateResolution(candidate, { online } = {}) {
  if (!candidate) return 'reject'
  if (candidate.source === 'draft') return 'reuse'
  if (candidate.source !== 'active') return 'reject'
  return online ? 'probe' : 'reuse'
}

/** Keep an early fresh edit separate when durable discovery arrives late. */
export function reconcileHydratedNewChatCandidate(
  currentCandidate,
  hydratedCandidate,
  { leaseWasEdited = false } = {},
) {
  if (!hydratedCandidate) return { candidate: currentCandidate, primeLease: false }
  if (!leaseWasEdited) return { candidate: hydratedCandidate, primeLease: true }
  if (hydratedCandidate.source === 'draft' && !currentCandidate?.draft) {
    return { candidate: null, primeLease: false }
  }
  return { candidate: currentCandidate, primeLease: false }
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

/** Classify a fresh detail probe without turning uncertainty into fake data. */
export function reusableChatDetailVerdict({ ok, status, detail }) {
  if (status === 404) return 'missing'
  if (!ok) return 'uncertain'
  if (detailIsUntouchedEmptyChat(detail)) return 'empty'
  // A successful response is safe to call occupied only when its runtime
  // shape is complete. Malformed/partial JSON is uncertainty, not evidence
  // that the row has messages.
  if (!detail || typeof detail !== 'object') return 'uncertain'
  if (!Number.isInteger(detail.total)) return 'uncertain'
  if (!Array.isArray(detail.messages)) return 'uncertain'
  if (!Array.isArray(detail.pending_messages)) return 'uncertain'
  if (typeof detail.running !== 'boolean') return 'uncertain'
  if (!Object.hasOwn(detail, 'pending_question_id')) return 'uncertain'
  if (!Object.hasOwn(detail, 'session_id')) return 'uncertain'
  return 'occupied'
}

/** Convert a complete create response into ChatView's persisted cache shape.
 * Older/local backends that return only the historical summary fail closed and
 * keep the existing detail fetch path. */
export function createdChatDetailCache(created) {
  const detail = created?.detail
  if (!detailIsUntouchedEmptyChat(detail)) return null
  if (typeof detail.provider !== 'string') return null
  if (!Number.isInteger(detail.offset) || detail.offset !== 0) return null
  if (!detail.effective_agent_settings
      || typeof detail.effective_agent_settings !== 'object') return null
  if (typeof detail.has_assistant_turns !== 'boolean') return null

  return {
    restorationWindowComplete: true,
    updated_at: typeof detail.updated_at === 'string'
      ? detail.updated_at
      : null,
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
      auto_resume_on_restart: !!detail.auto_resume_on_restart,
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

// A NetworkFirst drawer read can fall back to the service worker's previous
// list just after POST /chats succeeds. Keep the create response protected for
// one bounded handoff window so that fallback cannot erase the new row. The
// guard is Shell-owned (not global state); an explicit delete removes it.
export const CREATED_CHAT_LIST_GUARD_MS = 30_000

export function rememberCreatedChat(guards, created, {
  now = Date.now(),
  guardMs = CREATED_CHAT_LIST_GUARD_MS,
} = {}) {
  if (!guards || !created?.id) return
  const row = addCreatedChatToList([], created)[0]
  guards.set(String(created.id), {
    row,
    expiresAt: now + guardMs,
  })
}

/** Keep the bounded create guard aligned with an authoritative detail probe. */
export function reconcileCreatedChatGuard(guards, chatId, verdict) {
  const id = String(chatId || '')
  if (!id || !guards) return
  if (verdict === 'missing') {
    guards.delete(id)
    return
  }
  if (verdict !== 'occupied') return
  const guard = guards.get(id)
  if (!guard?.row) return
  guard.row = { ...guard.row, has_messages: true }
}

export function mergeChatListWithCreatedGuards(incoming, guards, {
  now = Date.now(),
} = {}) {
  let merged = Array.isArray(incoming) ? incoming : []
  if (!guards?.size) return merged
  for (const [id, guard] of guards) {
    if (!guard || guard.expiresAt <= now) {
      guards.delete(id)
      continue
    }
    const confirmedIndex = merged.findIndex(row => String(row?.id) === id)
    if (confirmedIndex >= 0) {
      const confirmed = merged[confirmedIndex]
      const guardedAt = Date.parse(guard.row?.updated_at || '')
      const confirmedAt = Date.parse(confirmed?.updated_at || '')
      const guardedIsNewer = Number.isFinite(guardedAt)
        && Number.isFinite(confirmedAt)
        && guardedAt > confirmedAt
      const preferred = guardedIsNewer ? guard.row : confirmed
      // During this short post-create window, a chat cannot become untouched
      // again. Keep has_messages monotonic even when a stale-present SW row
      // arrives after an authoritative detail probe or newer list response.
      const reconciled = {
        ...preferred,
        has_messages: !!(
          guard.row?.has_messages || confirmed?.has_messages
        ),
      }
      guard.row = reconciled
      merged = [...merged]
      merged[confirmedIndex] = reconciled
      continue
    }
    merged = addCreatedChatToList(merged, guard.row)
  }
  return merged
}
