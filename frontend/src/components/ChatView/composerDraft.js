function availableStorage(storage) {
  if (storage !== undefined) return storage
  try { return globalThis.sessionStorage ?? null } catch { return null }
}

function evictOneDraft(storage) {
  try {
    const draftKeys = []
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i)
      if (key?.startsWith('draft:')) draftKeys.push(key)
    }
    if (draftKeys.length === 0) return
    // Chat ids may be integers or UUIDs. Stable lexical order is sufficient:
    // quota recovery only needs to free one older best-effort draft.
    draftKeys.sort()
    storage.removeItem(draftKeys[0])
  } catch { /* best-effort storage */ }
}

/**
 * Persist a composer value immediately.
 *
 * This deliberately belongs on the input event path rather than only in a
 * React effect. A browser back gesture can commit navigation and unmount the
 * chat before passive effects run, especially while the mobile keyboard is
 * settling. Synchronous storage here makes the text durable before React gets
 * a chance to remove the composer.
 */
export function persistComposerDraft(chatId, input, storage) {
  const target = availableStorage(storage)
  if (!target || chatId == null) return false
  const key = `draft:${chatId}`

  try {
    if (input) target.setItem(key, input)
    else target.removeItem(key)
    return true
  } catch (error) {
    if (error?.name !== 'QuotaExceededError' && error?.code !== 22) return false
    evictOneDraft(target)
    try {
      if (input) target.setItem(key, input)
      else target.removeItem(key)
      return true
    } catch {
      return false
    }
  }
}
