const STORE_VERSION = 1

export const SEND_ATTEMPT_MISSING_MESSAGE =
  'That message didn’t reach the chat. It’s safe here—send it again when ready.'

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

export function failedSendReconciliation(
  attempt,
  messages,
  pendingMessages,
  { reportMissing = false } = {},
) {
  if (!attempt) return { status: 'none' }
  if (sendAttemptIsDurable(attempt, messages, pendingMessages)) {
    return { status: 'durable', clearDraft: true, sendFailure: null }
  }
  return {
    status: 'missing',
    ...(reportMissing ? { sendFailure: SEND_ATTEMPT_MISSING_MESSAGE } : {}),
  }
}
