import { cidOf } from './chatRuntimeState.js'

/**
 * Decide what handleStop should re-send after a Stop, from the queued
 * snapshot it collapsed plus the set of pending cids the backend reports
 * it actually cleared (`cleared_pending_cids` on the /chat/stop response).
 *
 * This is the SINGLE source of truth for both Stop branches — the clean
 * stop (interrupt succeeded) and the timeout (interrupt's 2s bound
 * elapsed, runner still draining). They used to diverge: the timeout
 * branch ignored the cleared set and re-sent the full snapshot
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
 *   - non-empty, every cleared cid matches a snapshot entry: resend
 *     exactly those. The backend drain is all-or-nothing, so a non-empty
 *     set means none were promoted — the matched subset can't double-send.
 *   - non-empty but some cleared cid is unmatched: fall back to the full
 *     combined snapshot. A visible resend beats a silent loss.
 *
 * Pure and side-effect-free: handleStop owns the actual doSend.
 *
 * @param {Array<{ts?: number, cid?: string, content?: string, attachments?: Array}>} queuedSnapshot
 *   the pending-queue entries handleStop snapshotted before clearing.
 * @param {string[]|null|undefined} clearedPendingCids
 *   the backend's authoritative cleared set (or null for a legacy backend).
 * @param {{text: string, attachments: Array}} combined
 *   the precomputed full-snapshot fallback (text joined, attachments
 *   de-duped) — passed in so the caller and this function agree exactly
 *   on the join/de-dup rules.
 * @returns {{text: string, attachments: Array}} what to resend; empty
 *   text means "do not resend".
 */
export function resolveStopResend(queuedSnapshot, clearedPendingCids, combined) {
  if (!Array.isArray(clearedPendingCids)) {
    return { text: combined.text, attachments: combined.attachments }
  }
  const clearedSet = new Set(clearedPendingCids)
  const toResend = (queuedSnapshot || []).filter(
    m => clearedSet.has(cidOf(m)),
  )
  if (toResend.length !== clearedPendingCids.length) {
    // A cleared cid didn't match a snapshot entry. Resend everything rather
    // than drop it.
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
