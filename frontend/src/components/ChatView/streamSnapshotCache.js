import { perfTime } from '../../lib/perfProbe.js'

/*
 * Versioned sessionStorage cache for the currently visible streaming
 * assistant items. It is intentionally tiny and side-effect scoped: the
 * stream transport decides when to buffer/flush/clear; this file only owns
 * key format, legacy invalidation, and the lifecycle write-behind buffer.
 *
 * Synchronous sessionStorage serialization previously ran on every reveal
 * commit and could block a phone for a full frame. The latest snapshot now
 * stays in memory until a lifecycle or terminal boundary flushes it. Those
 * explicit flushes preserve remount/reconnect recovery without putting storage
 * work back on the frame-paced reveal path.
 */

export const STREAM_SNAPSHOT_VERSION = 2
const STREAM_SNAPSHOT_PREFIX = `chat-stream-items:v${STREAM_SNAPSHOT_VERSION}:`

// chatId -> { items, storage }. The single pending write per chat, latest-wins.
// Absent when nothing needs to be flushed at the next lifecycle boundary.
const pendingWrites = new Map()

export function streamSnapshotKey(chatId) {
  return `${STREAM_SNAPSHOT_PREFIX}${chatId}`
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

function performWrite(chatId, items, storage) {
  if (!storage || !chatId) return
  try {
    perfTime(
      'stream.snapshotWrite',
      () => storage.setItem(streamSnapshotKey(chatId), JSON.stringify(items)),
    )
  } catch {
    // Best-effort only. If sessionStorage is unavailable, the durable DB
    // partial plus SSE catch-up still reconstruct the stream.
  }
}

function dropPending(chatId) {
  const entry = pendingWrites.get(chatId)
  if (!entry) return null
  pendingWrites.delete(chatId)
  return entry
}

export function bufferStreamSnapshot(chatId, items, storage = defaultStorage()) {
  if (!storage || !chatId) return
  if (!Array.isArray(items) || items.length === 0) return

  // Record latest-wins without serializing. A lifecycle/terminal boundary calls
  // flushStoredStreamSnapshot synchronously before the state can disappear.
  const existing = pendingWrites.get(chatId)
  if (existing) {
    existing.items = items
    existing.storage = storage
    return
  }
  pendingWrites.set(chatId, { items, storage })
}

// Synchronously write this chat's pending snapshot, if any. This is called at
// every boundary where the reconnect cache must be durable NOW.
export function flushStoredStreamSnapshot(chatId) {
  const entry = dropPending(chatId)
  if (entry) performWrite(chatId, entry.items, entry.storage)
}

export function clearStoredStreamSnapshot(chatId, storage = defaultStorage()) {
  // Drop any buffered write first — a pending value must never
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

/**
 * Reclaim every regenerable stream snapshot in one storage area.
 *
 * Composer drafts are owner-authored data; stream snapshots are a remount cache
 * backed by the durable partial plus SSE catch-up. If Web Storage fills up,
 * callers may clear this cache before retrying an owner-data write. Pending
 * buffered writes for the same storage are dropped as part of the transaction
 * so they cannot immediately resurrect the bytes that were just reclaimed.
 */
export function reclaimStoredStreamSnapshots(storage = defaultStorage()) {
  if (!storage) return 0

  for (const [chatId, entry] of pendingWrites) {
    if (entry.storage === storage) dropPending(chatId)
  }

  let reclaimed = 0
  try {
    const keys = []
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index)
      if (key?.startsWith(STREAM_SNAPSHOT_PREFIX)) keys.push(key)
    }
    for (const key of keys) {
      storage.removeItem(key)
      reclaimed += 1
    }
  } catch {
    // Best-effort emergency cleanup. The draft store still has its independent
    // memory + IndexedDB path when Web Storage is unavailable.
  }
  return reclaimed
}

// Test-only: reset the module's write-behind state so specs run in isolation
// regardless of order.
export function _resetStreamSnapshotBufferForTests() {
  pendingWrites.clear()
}
