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

export function legacyStreamSnapshotKey(chatId) {
  return `chat-stream-items:${chatId}`
}

function defaultStorage() {
  return typeof sessionStorage === 'undefined' ? null : sessionStorage
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
    // Retire pre-v2 snapshots too. A v1 snapshot has no turn-boundary
    // identity and can contain text that was already sealed into the
    // transcript before a fast-forwarded user message.
    storage.removeItem(legacyStreamSnapshotKey(chatId))
  } catch {
    // Best-effort cache; ignore storage failures.
  }
}
