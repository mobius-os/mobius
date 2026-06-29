/**
 * Decide what handleStop should re-send after a Stop, from the queued
 * snapshot it collapsed plus the set of pending ts the backend reports
 * it actually cleared (`cleared_pending_ts` on the /chat/stop response).
 *
 * This is the SINGLE source of truth for both Stop branches — the clean
 * stop (interrupt succeeded) and the timeout (interrupt's 2s bound
 * elapsed, runner still draining). They used to diverge: the timeout
 * branch ignored clearedPendingTs and re-sent the full snapshot
 * unconditionally, which DUPLICATED a message the natural turn-end drain
 * had already consumed (and risked a duplicate follow-up run). Sharing
 * one pure function is what keeps the two paths from drifting again.
 *
 * Contract, by what the backend cleared:
 *   - null / undefined (legacy backend without the field): fall back to
 *     the full combined snapshot — preserve back-compat over precision.
 *   - [] (empty): the queue was already drained (e.g. the natural finish
 *     raced Stop). Nothing was cleared, so resend NOTHING — re-sending
 *     would duplicate the message. Returns { text: '', attachments: [] }.
 *   - non-empty, every cleared ts matches a snapshot entry: resend
 *     exactly those. The backend drain is all-or-nothing, so a non-empty
 *     set means none were promoted — the matched subset can't double-send.
 *   - non-empty but some cleared ts is unmatched (the snapshot still
 *     holds an OPTIMISTIC ts for a message whose queue-POST was in flight
 *     when Stop landed): fall back to the full combined snapshot. A
 *     visible resend beats a silent loss.
 *
 * Pure and side-effect-free: handleStop owns the actual doSend.
 *
 * @param {Array<{ts?: number, content?: string, attachments?: Array}>} queuedSnapshot
 *   the pending-queue entries handleStop snapshotted before clearing.
 * @param {number[]|null|undefined} clearedPendingTs
 *   the backend's authoritative cleared set (or null for a legacy backend).
 * @param {{text: string, attachments: Array}} combined
 *   the precomputed full-snapshot fallback (text joined, attachments
 *   de-duped) — passed in so the caller and this function agree exactly
 *   on the join/de-dup rules.
 * @returns {{text: string, attachments: Array}} what to resend; empty
 *   text means "do not resend".
 */
export function resolveStopResend(queuedSnapshot, clearedPendingTs, combined) {
  if (!Array.isArray(clearedPendingTs)) {
    return { text: combined.text, attachments: combined.attachments }
  }
  const clearedSet = new Set(clearedPendingTs)
  const toResend = (queuedSnapshot || []).filter(
    m => m.ts != null && clearedSet.has(m.ts),
  )
  if (toResend.length !== clearedPendingTs.length) {
    // A cleared ts didn't match a snapshot entry (in-flight optimistic
    // ts). Resend everything rather than drop it.
    return { text: combined.text, attachments: combined.attachments }
  }
  // Exact match — including the empty-set drain case, where toResend is
  // [] and text becomes '' so nothing is re-sent.
  const text = toResend
    .map(m => (m.content || '').trim())
    .filter(Boolean)
    .join('\n')
  const seen = new Set()
  const attachments = []
  for (const m of toResend) {
    for (const a of (m.attachments || [])) {
      if (a && a.name && !seen.has(a.name)) {
        seen.add(a.name)
        attachments.push(a)
      }
    }
  }
  return { text, attachments }
}
