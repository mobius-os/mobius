/* View acknowledgement for actionable work represented by the Brain's Changes row. */

const STORAGE_PREFIX = 'mobius:brain-changes-seen:v1:'

function browserStorage() {
  try { return globalThis.localStorage ?? null } catch { return null }
}

function storageKey(chatId) {
  if (chatId == null || chatId === '') return ''
  return `${STORAGE_PREFIX}${encodeURIComponent(String(chatId))}`
}

export function changesAttentionCursor(overview) {
  const needsAttention = overview?.lifecycleAvailable === true
    && (overview.needsAction === true || overview.workState === 'attention')
  if (!needsAttention) return ''
  const work = overview?.work || {}
  const workCursor = overview.workState === 'attention'
    ? [
        work.id || '',
        work.status || '',
        work.updated_at || work.finished_at || '',
      ].join(':')
    : ''
  return `${overview.workflowRevision || ''}||${workCursor}`
}

export function readSeenChangesAttention(chatId, storage = browserStorage()) {
  const key = storageKey(chatId)
  if (!key || !storage) return ''
  try { return storage.getItem(key) || '' } catch { return '' }
}

export function writeSeenChangesAttention(
  chatId,
  cursor,
  storage = browserStorage(),
) {
  const key = storageKey(chatId)
  if (!key || !cursor || !storage) return false
  try {
    storage.setItem(key, cursor)
    return true
  } catch {
    return false
  }
}

export function hasUnseenChangesAttention(cursor, seenCursor) {
  return Boolean(cursor && cursor !== seenCursor)
}
