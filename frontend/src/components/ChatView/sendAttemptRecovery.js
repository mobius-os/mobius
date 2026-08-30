const STORE_VERSION = 1

export const SEND_ATTEMPT_MISSING_MESSAGE =
  'That message didn’t reach the chat. It’s safe here—send it again when ready.'
export const SEND_ATTEMPT_UNCONFIRMED_MESSAGE =
  'Möbius couldn’t confirm whether that message reached the chat. It’s safe here, and retrying won’t duplicate it.'

function storageKey(chatId) {
  return `mobius:send-attempt:v${STORE_VERSION}:${chatId}`
}

function restorableAttachment(attachment) {
  return {
    id: attachment?.id || '',
    name: attachment?.name || '',
    size: Number(attachment?.size) || 0,
    mime_type: attachment?.mime_type || '',
    status: 'done',
    error: null,
    objectUrl: null,
  }
}

export function loadFailedSendAttempt(chatId) {
  if (!chatId) return null
  try {
    const raw = globalThis.sessionStorage?.getItem(storageKey(chatId))
    if (!raw) return null
    const saved = JSON.parse(raw)
    if (
      saved?.version !== STORE_VERSION
      || saved.chatId !== chatId
      || typeof saved.cid !== 'string'
      || !saved.cid
      || typeof saved.draftIdentity !== 'string'
      || typeof saved.text !== 'string'
    ) {
      return null
    }
    return {
      cid: saved.cid,
      draftIdentity: saved.draftIdentity,
      text: saved.text,
      attachments: Array.isArray(saved.attachments)
        ? saved.attachments.map(restorableAttachment)
        : [],
    }
  } catch {
    return null
  }
}

export function saveFailedSendAttempt(chatId, attempt) {
  if (!chatId || !attempt?.cid || !attempt?.draftIdentity) return
  try {
    globalThis.sessionStorage?.setItem(storageKey(chatId), JSON.stringify({
      version: STORE_VERSION,
      chatId,
      cid: attempt.cid,
      draftIdentity: attempt.draftIdentity,
      text: String(attempt.text || ''),
      attachments: (attempt.attachments || []).map(restorableAttachment),
    }))
  } catch { /* private browsing / storage quota */ }
}

export function clearFailedSendAttempt(chatId) {
  if (!chatId) return
  try { globalThis.sessionStorage?.removeItem(storageKey(chatId)) } catch {}
}

export function sendAttemptIsDurable(attempt, messages, pendingMessages) {
  if (!attempt?.cid) return false
  return [...(messages || []), ...(pendingMessages || [])]
    .some(message => message?.role === 'user' && message.cid === attempt.cid)
}

export function sameSendAttempt(first, second) {
  return !!first
    && !!second
    && first.cid === second.cid
    && first.draftIdentity === second.draftIdentity
}

export function failedSendReconciliation(
  attempt,
  messages,
  pendingMessages,
  {
    reportMissing = false,
    reportUnavailable = false,
    expectedAttempt,
  } = {},
) {
  if (expectedAttempt !== undefined && !sameSendAttempt(attempt, expectedAttempt)) {
    return { status: attempt ? 'superseded' : 'none' }
  }
  if (!attempt) return { status: 'none' }
  if (sendAttemptIsDurable(attempt, messages, pendingMessages)) {
    return { status: 'durable', sendFailure: null }
  }
  return {
    status: reportUnavailable ? 'unconfirmed' : 'missing',
    ...(reportMissing ? { sendFailure: SEND_ATTEMPT_MISSING_MESSAGE } : {}),
    ...(reportUnavailable ? { sendFailure: SEND_ATTEMPT_UNCONFIRMED_MESSAGE } : {}),
  }
}

/**
 * Keep the provisional "Checking…" state until the bounded confirmation read
 * settles. A failed read cannot prove where the send landed, but it is the end
 * of this automatic check: hand the exact cid back to the existing
 * reconciliation owner, which either observes it locally or exposes
 * uncertainty-safe idempotent retry guidance with the restored draft intact.
 */
export async function settleFailedSendConfirmation(
  confirm,
  reconcileUnavailable,
  settleProvisional = null,
) {
  try {
    try {
      const confirmation = await confirm()
      if (confirmation !== null) return confirmation
    } catch {
      // Confirmation is a best-effort network read. Its user-facing fallback is
      // the same for an explicit null result and an unexpected rejection.
    }
    return reconcileUnavailable({ reportUnavailable: true })
  } finally {
    settleProvisional?.()
  }
}
