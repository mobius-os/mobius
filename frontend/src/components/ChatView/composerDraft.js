function availableStorage(storage) {
  if (storage !== undefined) return storage
  try { return globalThis.sessionStorage ?? null } catch { return null }
}

const DRAFT_ENVELOPE = 'mobius-composer-draft'

function restorableAttachments(attachments) {
  if (!Array.isArray(attachments)) return []
  return attachments
    .filter(attachment => (
      attachment
      && attachment.status !== 'uploading'
      && attachment.status !== 'error'
      && typeof attachment.name === 'string'
      && attachment.name.length > 0
    ))
    .map(attachment => ({
      name: attachment.name,
      size: Number.isFinite(attachment.size) ? attachment.size : 0,
      mime_type: typeof attachment.mime_type === 'string'
        ? attachment.mime_type
        : 'application/octet-stream',
    }))
}

/**
 * Reads either the current structured draft or a legacy plain-text draft.
 * Object URLs are deliberately not stored: they stop working when the chat
 * unmounts. Restored image cards point at the already-uploaded chat file.
 */
export function readComposerDraft(chatId, storage) {
  const target = availableStorage(storage)
  if (!target || chatId == null) return { input: '', attachments: [] }
  try {
    const raw = target.getItem(`draft:${chatId}`)
    if (!raw) return { input: '', attachments: [] }
    try {
      const parsed = JSON.parse(raw)
      if (parsed?.type === DRAFT_ENVELOPE && parsed.version === 1) {
        return {
          input: typeof parsed.input === 'string' ? parsed.input : '',
          attachments: restorableAttachments(parsed.attachments),
        }
      }
    } catch { /* legacy plain text */ }
    return { input: raw, attachments: [] }
  } catch {
    return { input: '', attachments: [] }
  }
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
export function persistComposerDraft(chatId, input, attachments = [], storage) {
  // Backward compatibility for callers of the former
  // persistComposerDraft(chatId, input, storage) signature.
  const legacyStorage = !Array.isArray(attachments) && storage === undefined
    ? attachments
    : undefined
  const draftAttachments = Array.isArray(attachments) ? attachments : []
  const target = availableStorage(storage ?? legacyStorage)
  if (!target || chatId == null) return false
  const key = `draft:${chatId}`
  const safeAttachments = restorableAttachments(draftAttachments)
  const value = safeAttachments.length > 0
    ? JSON.stringify({
        type: DRAFT_ENVELOPE,
        version: 1,
        input: typeof input === 'string' ? input : '',
        attachments: safeAttachments,
      })
    : (typeof input === 'string' ? input : '')

  try {
    if (value) target.setItem(key, value)
    else target.removeItem(key)
    return true
  } catch (error) {
    if (error?.name !== 'QuotaExceededError' && error?.code !== 22) return false
    evictOneDraft(target)
    try {
      if (value) target.setItem(key, value)
      else target.removeItem(key)
      return true
    } catch {
      return false
    }
  }
}
