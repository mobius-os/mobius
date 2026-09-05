function normalizedId(value) {
  return value == null ? null : String(value)
}

/** Persist an autosend handoff and prove it is readable before claiming it. */
export function stageVerifiedNewChatHandoff(chatId, input, {
  stageHandoff,
  readHandoff,
} = {}) {
  const text = typeof input === 'string' ? input : ''
  if (!text.trim() || typeof stageHandoff !== 'function'
      || typeof readHandoff !== 'function') return false

  stageHandoff(chatId, text, { autoSend: true })
  return readHandoff(chatId)?.autoSendDraft === text
}

/** A failed allocation retries only after the shared owner proves recovery. */
export function shouldRetryNewChatAllocation(presentation, recoveryGeneration) {
  if (!presentation?.failure || presentation.failure === 'queue') return false
  if (presentation.materialized) return false
  return recoveryGeneration > (presentation.failedAtRecoveryGeneration ?? 0)
}

/** Row-owned reads stay dormant until the client-minted chat exists server-side. */
export function newChatServerReadsReady(newChatSession) {
  return !newChatSession || newChatSession.materialized === true
}

/** Preserve live presentation state when an asynchronous allocation fails. */
export function failedNewChatPresentation(current, verdict, recoveryGeneration) {
  return {
    ...current,
    failure: verdict === 'offline' ? 'offline' : 'error',
    failedAtRecoveryGeneration: recoveryGeneration,
  }
}

/** Whether an allocating New Chat still owns the canonical destination. */
export function newChatPresentationIsCurrent(presentation, {
  viewMode,
  activeView,
  activeChatId,
  focusedPaneId,
  paneActiveKey,
} = {}) {
  if (!presentation || presentation.viewMode !== viewMode) return false
  // The client id is the final route from the first commit, before and after
  // allocation. Drawer state and history epochs are therefore irrelevant: the
  // presentation lives exactly as long as its canonical ChatView remains the
  // active destination.
  if (activeView !== 'chat') return false
  if (normalizedId(activeChatId) !== normalizedId(presentation.chatId)) return false
  if (presentation.viewMode !== 'panes') return true
  // Builder additionally owns one exact focused pane/tab. Focusing another
  // pane or selecting another tab is a newer intent even when the global route
  // still names a chat.
  return normalizedId(presentation.paneId) === normalizedId(focusedPaneId)
    && normalizedId(presentation.paneActiveKey) === normalizedId(paneActiveKey)
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
 * Return the only client-side chat that can still be treated as the visible
 * untouched compose surface.
 *
 * An off-screen empty row may belong to another browser that has just sent a
 * message while this tab's list cache is stale. Reusing it makes an explicit
 * "New chat" tap open that running conversation. The current chat is
 * different: keeping an already-open blank open is the intended no-op that
 * prevents repeated taps from manufacturing duplicate blanks. Deferred
 * navigation may also capture this identity, but validates it before routing.
 */
export function currentReusableEmptyChat(chats, {
  activeChatId,
  recoveredChatIds = new Set(),
  streamingChatIds = new Set(),
} = {}) {
  if (activeChatId == null) return null

  const activeId = normalizedId(activeChatId)
  const recoveredIds = new Set([...recoveredChatIds].map(normalizedId))
  const streamingIds = new Set([...streamingChatIds].map(normalizedId))

  const chat = (chats || []).find(row => normalizedId(row?.id) === activeId)
  if (!chat || chat.has_messages) return null
  if (recoveredIds.has(activeId) || streamingIds.has(activeId)) return null
  if (chat.running) return null
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
  if (!Object.hasOwn(detail, 'created_by_app_id')) return false
  if (detail.created_by_app_id != null) return false
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
  if (!Object.hasOwn(detail, 'created_by_app_id')) return 'uncertain'
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

// ── Client-minted New Chat intent id ─────────────────────────────────────────
// One id owns a New Chat from the first keystroke: it is BOTH the pre-chat draft
// key (`draft:<id>`) AND the eventual real chat id. POST /chats accepts a
// client-minted UUID idempotently (backend chats.py), so the composer can render
// and persist before the server row exists — no pre-id -> real-id migration, and
// returning to New Chat resumes the same draft under the same id.

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const NEW_CHAT_INTENT_KEY = 'new-chat-intent'

function sessionStore(storage) {
  if (storage !== undefined) return storage
  try { return globalThis.sessionStorage ?? null } catch { return null }
}

export function validNewChatIntentId(value) {
  return typeof value === 'string' && UUID_RE.test(value)
}

/**
 * Mint the client id for a New Chat intent. Prefer the platform UUID; fall back
 * to a manual v4 for older/embedded runtimes without `crypto.randomUUID`, or if
 * it throws. `randomUUID` is injectable for tests.
 */
export function mintNewChatIntentId({ randomUUID } = {}) {
  const mint =
    randomUUID || globalThis.crypto?.randomUUID?.bind(globalThis.crypto)
  if (mint) {
    try {
      const id = mint()
      if (typeof id === 'string' && UUID_RE.test(id)) return id.toLowerCase()
    } catch { /* fall through to the manual mint */ }
  }
  const bytes = new Uint8Array(16)
  let secure = false
  try {
    if (typeof globalThis.crypto?.getRandomValues === 'function') {
      globalThis.crypto.getRandomValues(bytes)
      secure = true
    }
  } catch { /* the manual fallback below remains valid */ }
  if (!secure) {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = (Math.random() * 256) | 0
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}`
    + `-${hex.slice(16, 20)}-${hex.slice(20)}`
}

/**
 * The id a New Chat surface should own right now. A still-open intent keeps its
 * id even when the synchronous Web Storage mirror is empty: the independent
 * durable draft may hydrate asynchronously under that same key. Server
 * materialization is not completion; the intent retires only when its first
 * message is accepted (or the chat is explicitly removed). An authoritative
 * occupied/tombstoned create verdict rotates the id before any row is adopted.
 */
export function resolveNewChatIntentId(pending, { randomUUID } = {}) {
  if (pending
      && pending.chatId != null
      && pending.status !== 'retired'
      && validNewChatIntentId(String(pending.chatId))) {
    return String(pending.chatId).toLowerCase()
  }
  return mintNewChatIntentId({ randomUUID })
}

/**
 * Reconcile an idempotent create response against the intent's id. Only a
 * verified-untouched ('empty') row is adopted. Confirmed occupancy (or a
 * tombstoned id) rotates after the caller copies the local draft. Missing,
 * uncertain, and transport-failed outcomes keep the SAME id for an idempotent
 * retry: manufacturing a second id after a lost create response would strand the
 * row that may already have committed and detach its draft.
 */
export function reconcileNewChatIntentCreate(intentId, verdict, { randomUUID } = {}) {
  const id = String(intentId).toLowerCase()
  if (verdict === 'empty') return { action: 'accept', chatId: id }
  if (verdict === 'occupied' || verdict === 'tombstoned') {
    return { action: 'rotate', chatId: mintNewChatIntentId({ randomUUID }) }
  }
  return { action: 'retry', chatId: id }
}

/** One tab-scoped pointer makes an unmounted draft discoverable after Back or reload. */
export function readNewChatIntent(storage) {
  const target = sessionStore(storage)
  if (!target) return null
  try {
    const parsed = JSON.parse(target.getItem(NEW_CHAT_INTENT_KEY) || 'null')
    if (!validNewChatIntentId(parsed?.chatId)) return null
    if (!['allocating', 'failed', 'materialized'].includes(parsed?.status)) return null
    return { chatId: String(parsed.chatId).toLowerCase(), status: parsed.status }
  } catch {
    return null
  }
}

export function writeNewChatIntent(intent, storage) {
  const target = sessionStore(storage)
  if (!target || !validNewChatIntentId(intent?.chatId)) return false
  const status = ['allocating', 'failed', 'materialized'].includes(intent.status)
    ? intent.status
    : 'allocating'
  try {
    target.setItem(NEW_CHAT_INTENT_KEY, JSON.stringify({
      chatId: String(intent.chatId).toLowerCase(),
      status,
    }))
    return true
  } catch {
    return false
  }
}

/** Clear only the pointer still owned by this chat; a newer intent always wins. */
export function clearNewChatIntent(chatId, storage) {
  const target = sessionStore(storage)
  if (!target) return false
  try {
    const current = readNewChatIntent(target)
    if (chatId != null
        && String(current?.chatId ?? '') !== String(chatId).toLowerCase()) return false
    target.removeItem(NEW_CHAT_INTENT_KEY)
    return true
  } catch {
    return false
  }
}
