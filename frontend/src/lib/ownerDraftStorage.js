const OWNER_DRAFT_PREFIXES = [
  'qa-draft:',
  'draft:',
  // Retire handoffs left by a tab that was open during the storage migration.
  'draft-autosend:',
  // Failed attempts retain both the visible draft and its exact hidden replay
  // context. Match the stable namespace so future record versions leave too.
  'mobius:send-attempt:',
]
const OWNER_DRAFT_KEYS = new Set([
  'composer-handoff',
  'new-chat-intent',
  'pending-draft',
  'pending-draft-autosend',
])


function browserStorages() {
  const stores = []
  try {
    if (globalThis.localStorage) stores.push(globalThis.localStorage)
  } catch { /* storage blocked */ }
  try {
    if (globalThis.sessionStorage && !stores.includes(globalThis.sessionStorage)) {
      stores.push(globalThis.sessionStorage)
    }
  } catch { /* storage blocked */ }
  return stores
}


function isOwnerDraftKey(key) {
  return OWNER_DRAFT_KEYS.has(key)
    || OWNER_DRAFT_PREFIXES.some(prefix => key.startsWith(prefix))
}


/** Remove unfinished owner-authored text when an owner session ends. */
export function clearOwnerDraftStorage(storages = browserStorages()) {
  for (const storage of storages) {
    try {
      const matches = []
      for (let index = 0; index < storage.length; index++) {
        const key = storage.key(index)
        if (key && isOwnerDraftKey(key)) matches.push(key)
      }
      for (const key of matches) storage.removeItem(key)
    } catch { /* private browsing or disabled storage */ }
  }
}
