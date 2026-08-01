// Canonical cache projection for GET /api/chats/{id}. ChatView and the shell's
// bounded idle prefetch must agree on this shape so a prefetched chat mounts as
// a real warm chat rather than through a second, parallel cache convention.

// The detail endpoint and inactive-cache projection share one page boundary.
// This is not a device-dependent entry ceiling: twenty is the server's own
// recent-history page, so an inactive chat keeps exactly the useful instant
// first paint that a cold activation would fetch while loaded pagination stays
// owned by mounted readers only.
export const CHAT_DETAIL_WARM_MESSAGE_LIMIT = 20

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

// Cached history is always a valid first paint, including while an agent is
// active. The authoritative refresh and stream handshake still catch it up;
// hiding the cache until then merely turns network latency into navigation
// latency and defeats the purpose of keeping the snapshot.
export function chatEntryPhase(cached) {
  return cached ? 'cached' : 'history'
}

// A detail cache carries the Chat row version it was built from. Runtime reads
// expose the same version without hydrating transcript JSON, so a retained
// chat can prove that its already-painted messages are still current.
// Missing versions fail closed during rolling updates and use the full detail
// path once to seed the contract.
export function chatSnapshotMatchesRuntime(cached, runtime) {
  return typeof cached?.updated_at === 'string'
    && typeof runtime?.updated_at === 'string'
    && cached.updated_at === runtime.updated_at
}

export function chatDetailCacheValue(data = {}) {
  return {
    updated_at: typeof data.updated_at === 'string' ? data.updated_at : null,
    messages: Array.isArray(data.messages)
      ? data.messages.map(settledToolBlocks)
      : [],
    offset: data.offset || 0,
    running: !!data.running,
    activeGoalObjective: typeof data.active_goal_objective === 'string'
      ? data.active_goal_objective
      : '',
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
      auto_resume_on_restart: !!data.auto_resume_on_restart,
    },
  }
}

// Once a chat has no mounted reader, keep the same useful first-paint shape as
// the initial detail request rather than retaining every page the reader ever
// loaded. Advancing the offset by the removed prefix preserves exact server
// pagination when that chat is opened again.
export function compactChatDetailCacheValue(
  data,
  messageLimit = CHAT_DETAIL_WARM_MESSAGE_LIMIT,
) {
  if (
    !data
    || !Array.isArray(data.messages)
    || data.messages.length <= messageLimit
  ) return data
  const removedCount = data.messages.length - messageLimit
  return {
    ...data,
    messages: data.messages.slice(-messageLimit),
    offset: Math.max(0, Number(data.offset) || 0) + removedCount,
  }
}
