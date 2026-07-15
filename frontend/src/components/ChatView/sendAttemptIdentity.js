function attachmentIdentity(attachment) {
  return [
    attachment?.name || '',
    Number(attachment?.size) || 0,
    attachment?.mime_type || '',
  ]
}

export function sendDraftIdentity(chatId, text, attachments) {
  return JSON.stringify([
    String(chatId || ''),
    String(text || ''),
    (attachments || []).map(attachmentIdentity),
  ])
}

export function cidForSendAttempt({ failedAttempt, draftIdentity, mintCid }) {
  if (
    failedAttempt?.cid
    && failedAttempt?.draftIdentity === draftIdentity
  ) {
    return failedAttempt.cid
  }
  return mintCid()
}
