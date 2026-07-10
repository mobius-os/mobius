/* System-event routing policy for events that arrive on a chat SSE stream. */

// Events that come through the chat SSE stream but are not chat content.
// These are published by agent scripts / watchers alongside normal chat
// events, so useStreamConnection forwards them to Shell.jsx instead of
// reducing them into assistant text/tool blocks.
export const CHAT_STREAM_SYSTEM_EVENTS = new Set([
  'theme_updated',
  'app_updated',
  // app_built is the chat-SCOPED CTA signal: the backend publishes it onto
  // only the building chat's broadcast (see routes/notify.py), so it arrives
  // exclusively on the stream of the chat that built the app. The handler sets
  // the "Open app" CTA. (app_updated stays list-refresh-only.)
  'app_built',
  'shell_rebuilding',
  'shell_rebuilt',
  'shell_apply_now',
  'shell_rebuild_failed',
  'chat_run_started',
  'chat_run_finished',
])

const CATCH_UP_UNSAFE_SYSTEM_EVENTS = new Set([
  'shell_rebuilding',
  'shell_rebuilt',
  'shell_apply_now',
  'shell_rebuild_failed',
])

export function isChatStreamSystemEvent(type) {
  return CHAT_STREAM_SYSTEM_EVENTS.has(type)
}

export function shouldForwardChatStreamSystemEvent(event, { isCatchUp = false } = {}) {
  const type = typeof event === 'string' ? event : event?.type
  if (!isChatStreamSystemEvent(type)) return false

  // Chat broadcasts replay their event_log whenever ChatView mounts or
  // reconnects. Most chat-scoped system events are harmless or useful during
  // that replay, but shell rebuild events are live lifecycle notifications.
  // Replaying an old `shell_rebuilt` after the owner switches chats makes the
  // Shell believe a fresh build just landed and re-shows "Update ready" (or
  // auto-refreshes if the page looks idle). The persistent /api/events/system
  // stream already delivers the live rebuild event, so catch-up copies should
  // be ignored.
  if (isCatchUp && CATCH_UP_UNSAFE_SYSTEM_EVENTS.has(type)) return false
  return true
}
