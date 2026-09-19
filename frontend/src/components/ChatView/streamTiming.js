/**
 * Timing constants shared by the stream-connection hook and its Playwright
 * coverage. Kept dependency-free (no React import) so tests can import these
 * exact values directly instead of copying them as literals that can
 * silently drift from the real thresholds.
 */

// A hidden tab that comes back inside this window is usually a glance at
// the notification shade or an app switch. If the SSE socket has also read
// recently, keep it: tearing down a healthy stream is what makes quiet tool
// turns flash "Reconnecting…" on every foreground.
export const QUICK_WAKE_HIDDEN_MS = 5000

// Window during which a 204 from /stream after a send is a race
// (the SSE GET landed before chats_stream.py:POST /messages finished
// registering the broadcast) rather than "agent finished." The POST
// handler returns 202 only AFTER create_broadcast(chat_id) completes,
// so any 204 outside this window genuinely means there's no active
// turn left and the right move is a DB refresh. Inside the window,
// schedule a quick reconnect instead — refreshing here would wipe
// the optimistic user message before persistence catches up.
//
// 1.5s is the empirical headroom: round-trip + create_broadcast +
// scheduler hop are well under that on local + remote prod traffic.
export const BROADCAST_REGISTRATION_WINDOW_MS = 1500
