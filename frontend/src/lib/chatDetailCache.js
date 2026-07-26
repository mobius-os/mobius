// Canonical cache projection for GET /api/chats/{id}. ChatView and the shell's
// bounded idle prefetch must agree on this shape so a prefetched chat mounts as
// a real warm chat rather than through a second, parallel cache convention.

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

export function chatDetailCacheValue(data = {}) {
  return {
    messages: Array.isArray(data.messages)
      ? data.messages.map(settledToolBlocks)
      : [],
    offset: data.offset || 0,
    running: !!data.running,
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
