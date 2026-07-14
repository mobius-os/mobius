import { useState, useRef, useCallback } from 'react'
import { cidOf } from '../chatRuntimeState.js'

/**
 * Hook that owns the per-chat pending-message queue (the items shown
 * in the queued-tray above the composer) and ALL the legitimate
 * mutations against it. Encapsulates the setState/ref-mirror dance
 * that previously lived inline in ChatView.jsx at several call sites,
 * the natural drift between which is the bug class this hook exists
 * to prevent.
 *
 * @template {object} PendingMsg
 *
 * Pending message shape (persisted to the server as
 * {role, content, ts, cid, attachments?}):
 *   - role:        always 'user'
 *   - content:     string
 *   - cid:         string — the STABLE identity, minted once at compose
 *                  time (or `legacy-<ts>` for a pre-cid row). Never
 *                  changes across the optimistic→confirm display-ts
 *                  update, so QueuedMessages keeps the row's expanded
 *                  state instead of remounting under a new key. React
 *                  key, DOM pin target, queue cancel key, steer selection.
 *   - ts:          number (epoch ms) — DISPLAY/ORDERING metadata only.
 *                  Optimistic Date.now() until the POST acks, then the
 *                  server's canonical ts. Identity does NOT ride on it.
 *   - queued:      true (marker)
 *   - serverTs:    boolean — true once the server has confirmed the row
 *                  (a confirmQueued ack or a hydrate). An optimistic add
 *                  starts false. Read by ChatView's steer gate: only a
 *                  server-confirmed row can be force-steered.
 *   - position?:   number (server-assigned)
 *   - attachments?: array
 *
 * Critical contract: pendingMessagesRef.current MUST update
 * SYNCHRONOUSLY on every mutation. handleStop's
 * fetchGenRef.current++ / pendingMessagesRef.current = [] sequence
 * runs BEFORE the await on /chat/stop; if any mutation here only
 * scheduled a render, the natural onStreamEnd handler could read
 * stale ref contents and re-fire fetchMessages({force:true}),
 * overwriting the just-promoted partial.
 *
 * Reconcile contract: hydrate() is a MERGE, not a wholesale replace.
 * It must never drop an optimistic-only entry whose own persistence
 * POST has not yet committed — that write is racing the reconcile
 * read, so the server simply hasn't seen the entry yet, and dropping
 * it is the persist-but-vanish clobber race. Each optimistic entry is
 * tracked in-flight (inFlightCidsRef) from its add() until its
 * round-trip resolves (confirm, cancel, promote, or clear); hydrate
 * preserves an in-flight entry the server list omits, and only an
 * entry that is NOT in flight is dropped. Identity matching is now BY
 * CID — the server row carries the same cid the client minted, so
 * hydrate reconciles directly with no content-heuristic guessing.
 *
 * The `setPendingMessages` setter is intentionally NOT exposed — the
 * named operations cover every call site and forcing them through the
 * API is the encapsulation.
 *
 * @returns {{
 *   pendingMessages: PendingMsg[],
 *   pendingMessagesRef: React.MutableRefObject<PendingMsg[]>,
 *   add: (msg: PendingMsg, opts?: {inFlight?: boolean}) => void,
 *   confirmQueued: (cid: string, patch?: {ts?: number, position?: number, serverMsg?: object}) => void,
 *   promoteByCid: (cid: string) => PendingMsg | null,
 *   promoteAll: (cid?: string) => PendingMsg | null,
 *   promoteManyByCid: (cidList: string[]) => PendingMsg | null,
 *   cancelByCid: (cid: string) => void,
 *   hydrate: (serverList: Array<{ts: number, content: string, cid?: string, role?: string, attachments?: Array, position?: number}>, opts?: {preserveMissing?: boolean}) => void,
 *   markInFlight: (cid: string) => void,
 *   clearInFlight: (cid: string) => void,
 *   clear: () => void,
 * }}
 */

function _fromServerList(serverList) {
  return (serverList || []).map(m => ({
    ...m,
    cid: cidOf(m),
    queued: true,
    serverTs: true,
  }))
}

// Combine a promoted group into the single provider-facing continuation row
// (mirrors backend _combine_pending_messages): single-newline join + first-
// occurrence attachment dedup, keeping the head row's identity/ts.
function _combinePromoted(promotedGroup) {
  const first = promotedGroup[0]
  const attachments = []
  const seenAttachments = new Set()
  for (const msg of promotedGroup) {
    for (const att of msg.attachments || []) {
      const key = JSON.stringify([
        att.name || att.filename || '',
        att.url || att.path || '',
        att.size || 0,
        att.mime_type || att.type || '',
      ])
      if (seenAttachments.has(key)) continue
      seenAttachments.add(key)
      attachments.push(att)
    }
  }
  const promoted = {
    ...first,
    content: promotedGroup.map(m => m.content || '').filter(Boolean).join('\n'),
    ts: first.ts,
  }
  if (attachments.length > 0) promoted.attachments = attachments
  else delete promoted.attachments
  return promoted
}

export default function usePendingQueue(initialServerList = []) {
  const initialPending = _fromServerList(initialServerList)
  const [pendingMessages, setPendingMessages] = useState(initialPending)
  const pendingMessagesRef = useRef(initialPending)
  // Cids of optimistic entries whose persistence POST is still in flight
  // (added locally, server ts not yet confirmed). hydrate() consults this so a
  // reconcile-fetch that lands while the POST is unresolved does NOT wipe the
  // entry: an optimistic-only message whose own POST hasn't committed must
  // survive a hydrate, because the server simply hasn't seen it yet — dropping
  // it is the reconcile-clobber race. `add({inFlight:true})` enters a cid here;
  // every path that resolves the optimistic round-trip — confirm, cancel,
  // promote, or a wholesale clear — removes it. An entry NOT in this set is
  // either already server-confirmed or genuinely gone, so hydrate is free to
  // drop it (a cancelled message must not resurrect). This is orthogonal to
  // identity — it guards the POST-vs-reconcile race, not the ts-swap.
  const inFlightCidsRef = useRef(new Set())

  // Internal helper: synchronously update both the ref and React state. Every
  // public operation funnels through this so the "ref updates before render"
  // contract holds in one place.
  const apply = useCallback((updater) => {
    const next = typeof updater === 'function'
      ? updater(pendingMessagesRef.current)
      : updater
    pendingMessagesRef.current = next
    setPendingMessages(next)
  }, [])

  // add(msg, {inFlight}) — inFlight guards ONLY an optimistic entry whose POST
  // is racing the reconcile read. The optimistic queue path passes
  // inFlight:true; the fresh-send-becomes-queued path (already server-
  // confirmed) passes inFlight:false so a later hydrate that legitimately drops
  // the row (the server cleared the queue) cannot resurrect a phantom.
  const add = useCallback((msg, { inFlight = false } = {}) => {
    const cid = cidOf(msg)
    if (inFlight && cid != null) inFlightCidsRef.current.add(cid)
    apply(prev => [
      ...prev,
      {
        ...msg,
        cid: cid ?? msg.cid,
        position: msg.position ?? prev.length + 1,
        // serverTs marks an entry whose ts is the SERVER's, not an optimistic
        // Date.now(). An optimistic add starts unconfirmed; confirmQueued flips
        // it once the POST acks. The steer gate reads this.
        serverTs: msg.serverTs === true,
      },
    ])
  }, [apply])

  // confirmQueued(cid, {ts, position, serverMsg}) — the POST acked. Update the
  // DISPLAY fields (ts, position, canonical serverMsg content) on the row
  // matched BY CID; identity never changes, so there is no swap, no twin-
  // collapse, no reissued-ts guard. Clear the in-flight mark.
  const confirmQueued = useCallback((cid, { ts, position, serverMsg } = {}) => {
    inFlightCidsRef.current.delete(cid)
    apply(prev => prev.map(m => {
      if (cidOf(m) !== cid) return m
      const next = {
        ...m,
        ...(serverMsg && typeof serverMsg === 'object' ? serverMsg : {}),
        cid,
        queued: true,
        ts: ts ?? serverMsg?.ts ?? m.ts,
        serverTs: true,
      }
      if (position !== undefined) next.position = position
      return next
    }))
  }, [apply])

  const promoteByCid = useCallback((cid) => {
    const current = pendingMessagesRef.current
    const idx = cid != null
      ? current.findIndex(m => cidOf(m) === cid)
      : (current.length > 0 ? 0 : -1)
    if (idx < 0) return null
    const promoted = current[idx]
    if (promoted.cid != null) inFlightCidsRef.current.delete(promoted.cid)
    const rest = current.filter((_, i) => i !== idx)
    pendingMessagesRef.current = rest
    setPendingMessages(rest)
    return promoted
  }, [])

  const promoteAll = useCallback((cid) => {
    const current = pendingMessagesRef.current
    if (current.length === 0) return null
    const idx = cid != null ? current.findIndex(m => cidOf(m) === cid) : 0
    if (idx < 0) return null
    const promotedGroup = current.slice(idx)
    const kept = current.slice(0, idx)
    for (const m of promotedGroup) {
      if (m.cid != null) inFlightCidsRef.current.delete(m.cid)
    }
    const promoted = _combinePromoted(promotedGroup)
    pendingMessagesRef.current = kept
    setPendingMessages(kept)
    return promoted
  }, [])

  const promoteManyByCid = useCallback((cidList) => {
    const wanted = new Set((cidList || []).filter(c => c != null))
    if (wanted.size === 0) return null
    const current = pendingMessagesRef.current
    const promotedGroup = current.filter(m => wanted.has(cidOf(m)))
    if (promotedGroup.length === 0) return null
    const kept = current.filter(m => !wanted.has(cidOf(m)))
    for (const m of promotedGroup) {
      if (m.cid != null) inFlightCidsRef.current.delete(m.cid)
    }
    const promoted = _combinePromoted(promotedGroup)
    pendingMessagesRef.current = kept
    setPendingMessages(kept)
    return promoted
  }, [])

  const cancelByCid = useCallback((cid) => {
    // A cancelled entry is genuinely gone; drop its in-flight mark so it
    // cannot resurrect on the next hydrate.
    inFlightCidsRef.current.delete(cid)
    apply(prev => prev.filter(m => cidOf(m) !== cid))
  }, [apply])

  // Reconcile the queue against authoritative server state WITHOUT clobbering
  // an optimistic entry whose own POST is still in flight.
  //
  // The server list is authoritative for everything it has SEEN, but an
  // optimistic-only entry whose persistence POST has not yet committed is
  // invisible to the server precisely because its write is racing this read —
  // it is not "removed", it is "not yet there". Replacing the tray wholesale
  // would drop it (persist-but-vanish). So we keep any local entry that is
  // (a) still flagged in-flight AND (b) absent from the server list (matched by
  // cid), appending it after the reconciled server entries.
  //
  // Identity matching is BY CID: the server row carries the same cid the client
  // minted (or a legacy-<ts> derivation), so a server row reuses that exact
  // identity. No content-identity heuristic is needed anymore.
  const hydrate = useCallback((serverList, opts = {}) => {
    const local = pendingMessagesRef.current || []
    const serverRows = serverList || []
    const serverCidSet = new Set(serverRows.map(m => cidOf(m)))

    const reconciled = serverRows.map(m => {
      const cid = cidOf(m)
      // The server has now seen this row, so its optimistic round-trip is
      // resolved — clear any lingering in-flight mark for its cid.
      if (cid != null) inFlightCidsRef.current.delete(cid)
      return { ...m, cid, queued: true, serverTs: true }
    })
    const preservedInFlight = local.filter(m => {
      const cid = cidOf(m)
      return cid != null
        && inFlightCidsRef.current.has(cid)
        && !serverCidSet.has(cid)
    })
    const preservedCidSet = new Set(preservedInFlight.map(m => cidOf(m)))
    const preservedMissing = opts.preserveMissing
      ? local
          .filter(m => m && !serverCidSet.has(cidOf(m)) && !preservedCidSet.has(cidOf(m)))
          .map(m => ({
            // The server omitted this row during an active-turn/runtime
            // reconcile. Keep the human-visible text, but downgrade the row so
            // fast-forward cannot force-steer using a server ts the backend no
            // longer confirms. A later hydrate that includes the row re-confirms.
            ...m,
            serverTs: false,
            missingFromServer: true,
          }))
      : []
    const next = [...reconciled, ...preservedInFlight, ...preservedMissing]
    pendingMessagesRef.current = next
    setPendingMessages(next)
  }, [])

  const clear = useCallback(() => {
    inFlightCidsRef.current.clear()
    pendingMessagesRef.current = []
    setPendingMessages([])
  }, [])

  // Explicit in-flight controls. add() already marks an optimistic entry
  // in-flight and the standard resolve paths clear it, so most callers never
  // touch these. clearInFlight has one production caller: doSend's queue-path
  // fallthrough, which resolves the flag for a terminal streamSend status
  // (e.g. `not_steered`) that no confirm or cancel covers.
  const markInFlight = useCallback((cid) => {
    if (cid != null) inFlightCidsRef.current.add(cid)
  }, [])

  const clearInFlight = useCallback((cid) => {
    inFlightCidsRef.current.delete(cid)
  }, [])

  return {
    pendingMessages,
    pendingMessagesRef,
    add,
    confirmQueued,
    promoteByCid,
    promoteAll,
    promoteManyByCid,
    cancelByCid,
    hydrate,
    clear,
    markInFlight,
    clearInFlight,
  }
}
