/*
 * Versioned sessionStorage cache for the currently visible streaming
 * assistant items. It is intentionally tiny and side-effect scoped: the
 * stream transport decides when to read/write/clear, this file only owns
 * key format, legacy invalidation, and the multi-pane write throttle.
 *
 * Multi-pane perf budget (design §2 — "Perf budget", flagged by every
 * critique so it is a gate, not a hope). Each mounted ChatView serializes the
 * full non-empty stream snapshot on every setStreamItems, and the typewriter
 * fires that once per animation frame. With a single chat mounted that cost is
 * negligible and the write stays SYNCHRONOUS (byte-identical to the pre-throttle
 * behavior). But once TWO ChatViews stream at once, per-frame JSON.stringify×N
 * onto one origin-scoped sessionStorage becomes the profiled hot path on the
 * main thread. So whenever >1 chat is mounted, writes coalesce to a trailing
 * edge of >=250ms PER CHAT.
 *
 * A throttle alone would reintroduce partial-text ROLLBACK: the snapshot is the
 * remount/reconnect fallback (useStreamConnection.js readStoredStreamSnapshot),
 * so if the shell reloads or a pane unmounts with a coalesced write still
 * pending, the fallback would restore a stale frame. Hence the mandatory
 * synchronous FLUSH contract: the caller flushes the pending trailing write on
 * BEFORE_SHELL_RELOAD_EVENT, pagehide, unmount, terminal promotion (stream end),
 * and any visibility swap (a pane hidden). Latest-wins coalescing keeps it
 * lossless — the flush always writes the most recent items handed in.
 */

export const STREAM_SNAPSHOT_VERSION = 2

// Trailing-edge coalescing window applied only while >1 chat is mounted.
export const STREAM_SNAPSHOT_THROTTLE_MS = 250

// Count of mounted ChatView stream controllers. Registered/unregistered by
// useStreamConnection's mount effect. The throttle is a multi-pane-only
// cost control; with <=1 mount there is nothing to contend for.
let mountedChatCount = 0

// chatId -> { items, storage, timer }. The single pending trailing write per
// chat, latest-wins. Absent when nothing is coalesced for that chat.
const pendingWrites = new Map()

export function streamSnapshotKey(chatId) {
  return `chat-stream-items:v${STREAM_SNAPSHOT_VERSION}:${chatId}`
}

function defaultStorage() {
  try { return globalThis.sessionStorage ?? null } catch { return null }
}

export function readStoredStreamSnapshot(chatId, storage = defaultStorage()) {
  if (!storage || !chatId) return []
  try {
    const raw = storage.getItem(streamSnapshotKey(chatId))
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

// Register/unregister a mounted chat. Arms the throttle only once a SECOND chat
// is mounted, so single-pane behavior is unthrottled and byte-identical.
export function registerMountedChat() {
  mountedChatCount += 1
}
export function unregisterMountedChat() {
  mountedChatCount = Math.max(0, mountedChatCount - 1)
}
export function getMountedChatCount() {
  return mountedChatCount
}

function performWrite(chatId, items, storage) {
  if (!storage || !chatId) return
  try {
    storage.setItem(streamSnapshotKey(chatId), JSON.stringify(items))
  } catch {
    // Best-effort only. If sessionStorage is unavailable, the durable DB
    // partial plus SSE catch-up still reconstruct the stream.
  }
}

function dropPending(chatId) {
  const entry = pendingWrites.get(chatId)
  if (!entry) return null
  if (entry.timer != null) clearTimeout(entry.timer)
  pendingWrites.delete(chatId)
  return entry
}

export function writeStoredStreamSnapshot(chatId, items, storage = defaultStorage()) {
  if (!storage || !chatId) return
  if (!Array.isArray(items) || items.length === 0) return

  // Single/zero mount: synchronous, byte-identical to the pre-throttle path.
  if (mountedChatCount <= 1) {
    // A prior multi-pane pending write for this chat would be staler than the
    // synchronous one we're about to make; drop it so it can't clobber later.
    dropPending(chatId)
    performWrite(chatId, items, storage)
    return
  }

  // Multi-pane: coalesce to a trailing-edge write. Record latest-wins; the
  // timer performs the actual serialization at the end of the window, and any
  // lifecycle/visibility flush writes it synchronously before then.
  const existing = pendingWrites.get(chatId)
  if (existing) {
    existing.items = items
    existing.storage = storage
    return
  }
  const entry = { items, storage, timer: null }
  entry.timer = setTimeout(() => {
    const cur = pendingWrites.get(chatId)
    pendingWrites.delete(chatId)
    if (cur) performWrite(chatId, cur.items, cur.storage)
  }, STREAM_SNAPSHOT_THROTTLE_MS)
  pendingWrites.set(chatId, entry)
}

// Synchronously write this chat's pending trailing snapshot, if any. The flush
// half of the throttle contract: called at every boundary where a coalesced
// write must be durable NOW (design §2). No-op when nothing is pending (the
// single-mount path already wrote synchronously).
export function flushStoredStreamSnapshot(chatId) {
  const entry = dropPending(chatId)
  if (entry) performWrite(chatId, entry.items, entry.storage)
}

// Flush every pending chat. Used by page-global boundaries (pagehide,
// shell reload) that leave no chat behind.
export function flushAllStreamSnapshots() {
  for (const chatId of [...pendingWrites.keys()]) flushStoredStreamSnapshot(chatId)
}

export function clearStoredStreamSnapshot(chatId, storage = defaultStorage()) {
  // Drop any coalesced write first — a pending trailing write must never
  // resurrect a snapshot the transport just decided to clear (e.g. a fresh
  // send or terminal 204 wiping stale partial items).
  dropPending(chatId)
  if (!storage || !chatId) return
  try {
    storage.removeItem(streamSnapshotKey(chatId))
  } catch {
    // Best-effort cache; ignore storage failures.
  }
}

// Test-only: reset the module's throttle state so specs run in isolation
// regardless of order.
export function _resetStreamSnapshotThrottleForTests() {
  for (const chatId of [...pendingWrites.keys()]) dropPending(chatId)
  mountedChatCount = 0
}
