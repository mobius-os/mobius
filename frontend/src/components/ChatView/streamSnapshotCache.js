/*
 * Versioned sessionStorage cache for the currently visible streaming
 * assistant items. It is intentionally tiny and side-effect scoped: the
 * stream transport decides when to read/write/clear, this file only owns
 * key format and legacy invalidation.
 */

export const STREAM_SNAPSHOT_VERSION = 2

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

export function writeStoredStreamSnapshot(chatId, items, storage = defaultStorage()) {
  if (!storage || !chatId) return
  if (!Array.isArray(items) || items.length === 0) return
  try {
    storage.setItem(streamSnapshotKey(chatId), JSON.stringify(items))
  } catch {
    // Best-effort only. If sessionStorage is unavailable, the durable DB
    // partial plus SSE catch-up still reconstruct the stream.
  }
}

export function clearStoredStreamSnapshot(chatId, storage = defaultStorage()) {
  if (!storage || !chatId) return
  try {
    storage.removeItem(streamSnapshotKey(chatId))
  } catch {
    // Best-effort cache; ignore storage failures.
  }
}
