const DISMISSED_UPDATE_KEY = 'mobius:app-update-dismissed:'

export function appUpdateNoticeIdentity(check) {
  if (check?.update_available !== true) return null
  const digest = String(check.candidate_source_digest || '').trim().toLowerCase()
  return /^[0-9a-f]{64}$/.test(digest) ? digest : null
}

function getStorage() {
  try {
    return globalThis.localStorage || null
  } catch {
    return null
  }
}

function storageKey(appId) {
  return `${DISMISSED_UPDATE_KEY}${String(appId)}`
}

export function isAppUpdateDismissed(appId, digest, storage = getStorage()) {
  if (!digest || !storage) return false
  try {
    return storage.getItem(storageKey(appId)) === digest
  } catch {
    return false
  }
}

export function dismissAppUpdate(appId, digest, storage = getStorage()) {
  if (!digest || !storage) return false
  try {
    storage.setItem(storageKey(appId), digest)
    return true
  } catch {
    return false
  }
}
