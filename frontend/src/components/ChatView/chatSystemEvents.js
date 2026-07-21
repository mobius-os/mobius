/* System-event routing policy for events that arrive on a chat SSE stream. */

// Events that come through the chat SSE stream but are not chat content.
// These are published by agent scripts / watchers alongside normal chat
// events, so useStreamConnection forwards them to Shell.jsx instead of
// reducing them into assistant text/tool blocks.
//
// This set is deliberately catch-up-SAFE only. The shell-rebuild lifecycle
// events (shell_rebuilding/rebuilt/apply_now/rebuild_failed) and
// app_build_failed/app_update_stale are catch-up-UNSAFE — a chat reconnect
// replaying an old copy would fire a spurious shell apply or a stale recovery
// toast — so the backend keeps them on the system broadcast alone (no per-chat
// fan-out, no replay) and they never reach a chat stream at all.
export const CHAT_STREAM_SYSTEM_EVENTS = new Set([
  'theme_updated',
  'app_updated',
  // build_phase is the chat-SCOPED milestone signal: the backend publishes it
  // onto only the building chat's broadcast (see routes/notify.py), so it
  // arrives exclusively on the stream of the chat that is building. ChatView
  // accumulates it into the live phase rail. It is REPLAY-SAFE on purpose so a
  // reconnect's catch-up burst rebuilds the rail; the rail state dedupes by ts,
  // so replaying a phase never double-counts it.
  'build_phase',
  'chat_run_started',
  'chat_run_finished',
])

export function isChatStreamSystemEvent(type) {
  return CHAT_STREAM_SYSTEM_EVENTS.has(type)
}

// Every event in CHAT_STREAM_SYSTEM_EVENTS is catch-up-safe, so forwarding is
// unconditional. (The old catch-up gate existed only for the shell-rebuild
// events, which no longer ride the chat stream — they are system-bus-only.)
export function shouldForwardChatStreamSystemEvent(event) {
  const type = typeof event === 'string' ? event : event?.type
  return isChatStreamSystemEvent(type)
}
