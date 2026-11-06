import { cidOf } from './messageIdentity.js'

/**
 * Owner-typed subset of a pending-queue snapshot for the Stop resend
 * contract. A `hidden` queued row is never owner speech — it is a
 * machine-owned carrier (wait/delegation/activation result, a peer-message
 * wake, or a secure-input answer continuation) parked behind the
 * owner-input barrier, each with its own idempotent delivery latch. Stop's
 * "collapse queued text into one fresh follow-up turn" only ever re-sends
 * what the owner typed; folding a hidden carrier into that turn re-sends it
 * as if the owner typed it — the phantom-queued-message bug. The backend
 * ClearPending mirror preserves these carriers and omits their cids from
 * cleared_pending_cids; filtering here keeps the legacy-null and cid-mismatch
 * fallback paths honest too.
 *
 * @param {Array<{hidden?: boolean}>} snapshot
 * @returns {Array} the owner-visible entries, order preserved.
 */
export function ownerQueuedSnapshot(snapshot) {
  return (snapshot || []).filter(m => m && !m.hidden)
}

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
 * Only meaningful once a stop request reached the server; when none did,
 * splitTrayAfterUnacceptedStop decides from the server's own queue instead.
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
  return collapseQueued(toResend)
}

/**
 * Queued rows as one message: their text joined by line, their files
 * de-duplicated by name.
 *
 * @param {Array<{content?: string, attachments?: Array}>} rows
 * @returns {{text: string, attachments: Array}}
 */
export function collapseQueued(rows) {
  const text = (rows || [])
    .map(m => (m.content || '').trim())
    .filter(Boolean)
    .join('\n')
  const seen = new Set()
  const attachments = []
  for (const m of rows || []) {
    for (const a of (m.attachments || [])) {
      if (a && a.name && !seen.has(a.name)) {
        seen.add(a.name)
        attachments.push(a)
      }
    }
  }
  return { text, attachments }
}

/**
 * After a Stop the server never acknowledged (a network error or a non-OK
 * reply), the server's own queue says what it still holds: the request may
 * have cleared it before the reply was lost. Stop already emptied the tray.
 *
 * - A row the server still holds goes back to the tray, as the server's copy.
 * - A row the server never confirmed (its queue POST is still in flight, or
 *   the durable outbox owns its replay) goes back to the tray too: that send
 *   still owns it, and returning it to the composer would duplicate it.
 * - A confirmed owner row the server no longer holds, and which has not
 *   already reached the transcript, returns to the composer: the server
 *   cleared it, so restoring it to the tray would only let the next refresh
 *   drop it silently.
 * - A hidden machine carrier the server no longer holds is gone; it is never
 *   owner text.
 *
 * When the server's queue could not be read (`serverPending` null), no
 * confirmed row is known to be held, so confirmed owner rows return to the
 * composer: at worst the owner sees a copy there beside the tray row the next
 * refresh restores, never a silent loss.
 *
 * @param {Array} tray the queue rows Stop cleared, in order
 * @param {Array|null} serverPending the server's pending_messages, or null
 * @param {Set<string>} deliveredCids cids already in the transcript
 * @returns {{restore: Array, returned: {text: string, attachments: Array}}}
 */
export function splitTrayAfterUnacceptedStop(tray, serverPending, deliveredCids = new Set()) {
  const held = new Map((serverPending || []).map(row => [cidOf(row), row]))
  const restore = []
  const returned = []
  for (const row of tray || []) {
    const cid = cidOf(row)
    if (held.has(cid)) {
      restore.push({ ...row, ...held.get(cid), cid, queued: true, serverTs: true, localOnly: false })
    } else if (row.serverTs !== true) {
      restore.push(row)
    } else if (!row.hidden && !deliveredCids.has(cid)) {
      returned.push(row)
    }
  }
  return { restore, returned: collapseQueued(returned) }
}
