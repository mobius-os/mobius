// Canonical cache projection for GET /api/chats/{id}: the shape ChatView mounts
// from on activation, kept as one cache convention rather than a second,
// parallel one.

// Ordinary background persistence keeps the same recent page a cold activation
// asks the server for. The explicit reload handoff deliberately bypasses this
// projection so the currently loaded reader can restore its exact coordinate.
export const CHAT_DETAIL_PERSISTED_MESSAGE_LIMIT = 20

export function messageKey(message, index = 0) {
  if (!message) return null
  if (message.id != null) return String(message.id)
  if (message.cid != null) return String(message.cid)
  return `${message.role}-${message.ts ?? index}`
}

/** The one pre-persistence address shared by a live assistant and its row. */
export function assistantAnchorKey(index) {
  return `assistant-${index}`
}

/** True when a durable address names this row through any identity the row
 * has carried. The primary DOM key stays id-first for existing positions;
 * cid and role/timestamp aliases bridge optimistic and legacy lifetimes. */
export function messageMatchesKey(message, index, key) {
  if (!message || key == null) return false
  const target = String(key)
  if (messageKey(message, index) === target) return true
  if (message.cid != null && String(message.cid) === target) return true
  const role = message.role
  if (message.ts != null && `${role}-${message.ts}` === target) return true
  return `${role}-${index}` === target
}

/** True when the durable transcript contains the named unanswered question. */
export function hasPendingQuestionMessage(messages, pendingQuestionId) {
  if (!pendingQuestionId || !Array.isArray(messages)) return false
  return messages.some(message => (
    message?.role === 'assistant'
    && (message.blocks || []).some(block => (
      block?.type === 'question'
      && block.question_id === pendingQuestionId
      && !block.answers
    ))
  ))
}

/** Classify the strongest safe entry a canonical detail cache can provide.
 *
 * A saved row must be present in the cached window. Exact nested parts are a
 * DOM fact rather than a data fact: mount those caches behind the reveal gate
 * as `validating`, then let the scroll owner promote them only when the saved
 * part resolves in the committed cached DOM. A running cache additionally
 * waits for subscribe-time replay because persisted rows can lag the active
 * assistant; parked owner questions have no possible stream output. */
export function chatCacheEntryState(
  cached,
  savedAnchorKey = null,
  savedAnchorHasNestedPart = false,
) {
  if (cached?.restorationWindowComplete !== true || !Array.isArray(cached.messages)) {
    return 'missing'
  }
  if (
    cached.pending_question_id
    && !hasPendingQuestionMessage(
      cached.messages,
      cached.pending_question_id,
    )
  ) {
    return 'missing'
  }
  let containsAnchor = savedAnchorKey == null
  if (!containsAnchor) {
    const baseOffset = Number.isInteger(cached.offset) ? cached.offset : 0
    containsAnchor = cached.messages.some((message, index) => (
      messageKey(message, baseOffset + index) === String(savedAnchorKey)
      || (
        message?.role === 'assistant'
        && !message.hidden
        && assistantAnchorKey(baseOffset + index) === String(savedAnchorKey)
      )
      || (
        message?.role === 'user'
        && !message.hidden
        && message.kind !== 'continuation'
        && message.kind !== 'auto_continuation'
        && message.cid != null
        && String(message.cid) === String(savedAnchorKey)
      )
    ))
  }
  if (!containsAnchor) return 'missing'
  if (savedAnchorHasNestedPart) return 'validating'
  return cached.running && !cached.pending_question_id ? 'stream-catchup' : 'paintable'
}

function settledToolBlocks(message) {
  const blocks = Array.isArray(message?.blocks) ? message.blocks : null
  if (!blocks?.some(block => block?.type === 'tool' && block.status === 'running')) {
    return message
  }
  return {
    ...message,
    blocks: blocks.map(block => (
      block?.type === 'tool' && block.status === 'running'
        ? { ...block, status: 'done' }
        : block
    )),
  }
}

// A detail cache carries the Chat row version it was built from. Runtime reads
// expose the same version without hydrating transcript JSON, so a retained
// chat can prove that its already-painted messages are still current. A
// pending-question marker additionally requires its actionable card: version
// equality cannot bless a truncated/poisoned transcript. Missing evidence
// fails closed and uses the full detail path once to seed the contract.
export function chatSnapshotMatchesRuntime(cached, runtime) {
  const sameVersion = typeof cached?.updated_at === 'string'
    && typeof runtime?.updated_at === 'string'
    && cached.updated_at === runtime.updated_at
  if (!sameVersion) return false
  return !runtime.pending_question_id
    || hasPendingQuestionMessage(
      cached.messages,
      runtime.pending_question_id,
    )
}

/** Decide whether an idle runtime read disproves the retained transcript.
 * Live local work owns the surface until it settles; otherwise a missing or
 * moved durable version requires the authoritative detail path. */
export function shouldRefetchTranscriptForRuntime(
  cached,
  runtime,
  localAuthoritative = false,
) {
  return !runtime?.running
    && !localAuthoritative
    && !chatSnapshotMatchesRuntime(cached, runtime)
}

export function chatDetailCacheValue(data = {}) {
  const sourceWindowValid = Array.isArray(data.messages)
    && Number.isInteger(data.offset)
    && data.offset >= 0
    && Number.isInteger(data.total)
    && data.total >= 0
  const messages = Array.isArray(data.messages)
    ? data.messages.map(settledToolBlocks)
    : []
  const offset = Number.isInteger(data.offset) && data.offset >= 0
    ? data.offset
    : 0
  const total = Number.isInteger(data.total) && data.total >= 0
    ? data.total
    : null
  return {
    // Reject persisted legacy/poisoned cache shapes on first paint. This marker
    // is minted only by a canonical window that reaches the current tail and
    // survives later local/stream updates through existing spread paths.
    restorationWindowComplete: sourceWindowValid
      && data.offset + messages.length === data.total,
    updated_at: typeof data.updated_at === 'string' ? data.updated_at : null,
    messages,
    total,
    offset,
    running: !!data.running,
    activeGoalObjective: typeof data.active_goal_objective === 'string'
      ? data.active_goal_objective
      : '',
    goal: data.goal && typeof data.goal === 'object' ? { ...data.goal } : null,
    pending_messages: Array.isArray(data.pending_messages)
      ? data.pending_messages
      : [],
    pending_question_id: data.pending_question_id || null,
    chatInfo: {
      provider: data.provider || 'claude',
      created_by_app_id: data.created_by_app_id ?? null,
      agent_settings_json: data.agent_settings_json || null,
      effective: data.effective_agent_settings || {},
      has_assistant_turns: !!data.has_assistant_turns,
      auto_resume_on_limit: !!data.auto_resume_on_limit,
    },
  }
}

export function compactPersistedChatDetailCacheValue(
  data,
  messageLimit = CHAT_DETAIL_PERSISTED_MESSAGE_LIMIT,
) {
  if (!data || !Array.isArray(data.messages) || data.messages.length <= messageLimit) {
    return data
  }
  const removedCount = data.messages.length - messageLimit
  return {
    ...data,
    messages: data.messages.slice(-messageLimit),
    offset: Math.max(0, Number(data.offset) || 0) + removedCount,
  }
}

/** Choose the query-cache window for an optimistic server-behind handoff.
 * A concurrent non-empty publication wins; an empty cache cannot erase the
 * mounted transcript merely because [] is truthy. */
export function optimisticHandoffWindow(existing, mountedMessages, mountedOffset) {
  if (Array.isArray(existing?.messages) && existing.messages.length > 0) {
    return {
      messages: existing.messages,
      offset: Number.isInteger(existing.offset) ? existing.offset : mountedOffset,
      restorationWindowComplete: existing.restorationWindowComplete === true,
    }
  }
  return {
    messages: mountedMessages,
    offset: mountedOffset,
    restorationWindowComplete: true,
  }
}

function durableMessageIdentity(message) {
  if (!message) return null
  if (message.id != null) return `id:${message.id}`
  if (message.cid != null) return `cid:${message.cid}`
  if (message.ts != null) return `ts:${message.role || ''}:${message.ts}`
  return null
}

export function mergeRecentMessagesIntoLoadedWindow({
  loadedMessages,
  loadedOffset,
  recentMessages,
  recentOffset,
  preserveLocalSuffix = false,
}) {
  const recent = Array.isArray(recentMessages) ? recentMessages : []
  const fallback = {
    messages: recent,
    offset: Number.isInteger(recentOffset) ? recentOffset : 0,
    verified: false,
  }
  if (!Array.isArray(loadedMessages) || loadedMessages.length === 0) {
    return { ...fallback, verified: true }
  }
  if (!Number.isInteger(loadedOffset) || !Number.isInteger(recentOffset)) return fallback

  const prefixLength = recentOffset - loadedOffset
  if (prefixLength < 0 || prefixLength > loadedMessages.length || recent.length === 0) {
    return fallback
  }
  const overlapLength = Math.min(loadedMessages.length - prefixLength, recent.length)
  if (overlapLength <= 0) return fallback
  for (let index = 0; index < overlapLength; index += 1) {
    const loadedId = durableMessageIdentity(loadedMessages[prefixLength + index])
    const recentId = durableMessageIdentity(recent[index])
    if (!loadedId || loadedId !== recentId) return fallback
  }
  const localSuffix = preserveLocalSuffix
    ? loadedMessages.slice(prefixLength + overlapLength)
    : []
  return {
    messages: [...loadedMessages.slice(0, prefixLength), ...recent, ...localSuffix],
    offset: loadedOffset,
    verified: true,
  }
}
