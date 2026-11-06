/**
 * A composer draft is sendable when it contains visible text or at least one
 * completed attachment. Live upload chips carry a status; attachment metadata
 * restored from storage or copied from a queued message is already complete
 * and therefore has no status.
 */
export function hasSendablePayload(text, attachments = []) {
  if (String(text || '').trim()) return true
  return (attachments || []).some(attachment => (
    attachment
    && typeof attachment.name === 'string'
    && attachment.name.length > 0
    && (attachment.status === undefined || attachment.status === 'done')
  ))
}

/**
 * Whether the queue waits for the owner's send: a Stop held it (the server
 * marks each held row `held`) and no turn is running. The composer then offers
 * an empty Send that runs the whole queue, instead of a Stop with nothing left
 * to stop.
 */
export function queueAwaitsOwnerSend({ turnActive, pendingMessages }) {
  return !turnActive && (pendingMessages || []).some(row => row?.held === true)
}
