/* Remembers, per chat and Goal, that the owner declined the auto-continue offer
   so a reload or remount does not ask again. */
import { localStore } from '../../lib/workspaceStorage.js'

const STORAGE_PREFIX = 'mobius:goal-auto-resume-dismissed:v1:'

function storageKey(chatId, goalKey) {
  if (!chatId || !goalKey) return ''
  return `${STORAGE_PREFIX}${encodeURIComponent(String(chatId))}:${encodeURIComponent(String(goalKey))}`
}

export function isGoalAutoResumeDismissed(chatId, goalKey, storage = localStore()) {
  const key = storageKey(chatId, goalKey)
  if (!key || !storage) return false
  try { return storage.getItem(key) === '1' } catch { return false }
}

/** Blocked or full storage leaves the dismissal session-only; the caller keeps it in state. */
export function rememberGoalAutoResumeDismissed(chatId, goalKey, storage = localStore()) {
  const key = storageKey(chatId, goalKey)
  if (!key || !storage) return
  try { storage.setItem(key, '1') } catch { /* session-only dismissal */ }
}
