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
