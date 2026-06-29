import { useState, useRef, useCallback } from 'react'

/**
 * Hook that owns the per-chat pending-message queue (the items shown
 * in the queued-tray above the composer) and ALL the legitimate
 * mutations against it. Encapsulates the setState/ref-mirror dance
 * that previously lived inline in ChatView.jsx at eight separate
 * call sites, the natural drift between which is the bug class
 * this hook exists to prevent.
 *
 * @template {object} PendingMsg
 *
 * Pending message shape (carried unchanged from the prior inline
 * code; persisted to the server only as {role, content, ts,
 * attachments?}):
 *   - role:        always 'user'
 *   - content:     string
 *   - ts:          number (epoch ms; server-assigned after POST, or
 *                  optimistic Date.now() until then)
 *   - cid:         string (stable client-side React key; survives
 *                  optimistic-ts -> server-ts swap so QueuedMessages
 *                  doesn't remount under a new key and lose UI state)
 *   - queued:      true (marker)
 *   - serverTs:    boolean — true once `ts` is the SERVER's (server-origin
 *                  `s-` cid, swapOptimisticTs ack, or a hydrate). An
 *                  optimistic add starts false. Read by ChatView's steer
 *                  gate: force-steer matches the server's
 *                  chat.pending_messages[].ts, so only a serverTs entry can
 *                  be steered (an unconfirmed optimistic ts would not match).
 *   - position?:   number (server-assigned)
 *   - attachments?: array
 *
 * Critical contract: pendingMessagesRef.current MUST update
 * SYNCHRONOUSLY on every mutation. handleStop's
 * fetchGenRef.current++ / pendingMessagesRef.current = [] sequence
 * runs BEFORE the await on /chat/stop; if any mutation here only
 * scheduled a render, the natural onStreamEnd handler could read
 * stale ref contents and re-fire fetchMessages({force:true}),
 * overwriting the just-promoted partial. R1 in _034-design.md
 * spells out the failure mode.
 *
 * Reconcile contract: hydrate() is a MERGE, not a wholesale replace.
 * It must never drop an optimistic-only entry whose own persistence
 * POST has not yet committed — that write is racing the reconcile
 * read, so the server simply hasn't seen the entry, and dropping it
 * is the persist-but-vanish clobber race. Each optimistic entry is
 * tracked in-flight from its add() until its round-trip resolves
 * (swap to a server ts, cancel, promote, or clear); hydrate preserves
 * an in-flight entry the server list omits, and only an entry that is
 * NOT in flight (server-confirmed or genuinely cancelled) is dropped.
 *
 * The `setPendingMessages` setter is intentionally NOT exposed —
 * the five named operations cover every call site enumerated in
 * the design and forcing them through the API is the encapsulation.
 *
 * @returns {{
 *   pendingMessages: PendingMsg[],
 *   pendingMessagesRef: React.MutableRefObject<PendingMsg[]>,
 *   add: (msg: PendingMsg) => void,
 *   swapOptimisticTs: (cid: string, serverTs: number, position?: number, serverMsg?: object, opts?: {confirmed?: boolean}) => void,
 *   promoteByTs: (ts: number) => PendingMsg | null,
 *   promoteAll: (ts: number) => PendingMsg | null,
 *   promoteManyByTs: (tsList: number[]) => PendingMsg | null,
 *   cancelByTs: (ts: number) => void,
 *   cancelByCid: (cid: string) => void,
 *   hydrate: (serverList: Array<{ts: number, content: string, role?: string, attachments?: Array, position?: number}>, opts?: {preserveMissing?: boolean}) => void, // MERGE: server list + any local entry that must not be silently dropped
 *   markInFlight: (cid: string) => void,
 *   clearInFlight: (cid: string) => void,
 *   clear: () => void,
 * }}
 *
 * Note: `cancelByCid` exists alongside `cancelByTs` because the
 * optimistic-add → server-confirm round-trip has two failure modes
 * keyed by different identifiers: rollback-on-error (the optimistic
 * never got a server ts; cid is the only handle) and the
 * server-said-started removal (also pre-swap, also cid-keyed). The
 * design enumerated five ops, but two of the call sites listed
 * under "add(...)" (lines 719-723, 742-746) are actually cid-keyed
 * removes; cancelByCid is the faithful mapping.
 */

function _attachmentKey(att) {
  return JSON.stringify([
    att?.name || att?.filename || '',
    att?.url || att?.path || '',
    att?.size || 0,
    att?.mime_type || att?.type || '',
  ])
}

function _queueIdentityKey(msg) {
  return JSON.stringify([
    String(msg?.content || '').trim(),
    (msg?.attachments || []).map(_attachmentKey).sort(),
  ])
}

function _fromServerList(serverList) {
  return (serverList || []).map(m => ({
    ...m,
    cid: m.cid || `s-${m.ts}`,
    queued: true,
    serverTs: true,
  }))
}

export default function usePendingQueue(initialServerList = []) {
  const initialPending = _fromServerList(initialServerList)
  const [pendingMessages, setPendingMessages] = useState(initialPending)
  const pendingMessagesRef = useRef(initialPending)
  // Deferred-removal guard: `Map<consumedTs, Set<cid>>`. When a started turn
  // consumes a server ts whose OPTIMISTIC entry is still in flight (its POST
  // not yet acked, so the entry carries a client ts, not this server ts, and
  // can't be removed by ts), we record the consumed ts mapped to the set of
  // in-flight cids that EXIST at that moment — the only cids that could
  // legitimately resolve to it. swapOptimisticTs removes the entry ONLY when
  // its cid is in that set.
  //
  // Keying by cid (not ts alone) is what makes a server-REISSUED ts safe: when
  // _ensure_unique_ts frees a ts and hands it to a NEW queued message, that
  // message's cid was NOT in flight at arm time, so it is absent from the
  // guard's cid set and its swap is NOT dropped (the "didn't show up as queued"
  // bug, #4). A bare ts-keyed guard dropped it.
  //
  // Bounded so a stale entry can't accumulate:
  //   - armed only for a consumed ts ABSENT from the list AND only while a cid
  //     is in flight (see promoteManyByTs);
  //   - PURGED the moment no optimistic POST is in flight (inFlightCidsRef
  //     empty) — no pending swap can then match it.
  // NOT cleared in hydrate: a hydrate can land WHILE a legitimate in-flight
  // consume is pending (its cid still in flight), and dropping the guard then
  // would resurrect the consumed message as a visible chip on the next swap.
  const consumedServerTsRef = useRef(new Map())
  // Cids of optimistic entries whose persistence POST is still in
  // flight (added locally, server ts not yet confirmed). hydrate()
  // consults this so a reconcile-fetch that lands while the POST is
  // unresolved does NOT wipe the entry: an optimistic-only message
  // (cid present, no matching server ts) whose own POST hasn't
  // committed must survive a hydrate, because the server simply
  // hasn't seen it yet — dropping it is the reconcile-clobber race
  // (a Stop-timeout resend racing onStreamEnd's fetchMessages was one
  // way to hit it). `add` enters a cid here; every path that resolves
  // the optimistic round-trip — swap to a server ts, cancel, promote,
  // or a wholesale clear — removes it. An entry NOT in this set is
  // either already server-confirmed or genuinely gone, so hydrate is
  // free to drop it (a cancelled message must not resurrect).
  const inFlightCidsRef = useRef(new Set())

  // Internal helper: synchronously update both the ref and React
  // state. Every public operation funnels through this so the
  // "ref updates before render" contract holds in one place.
  const apply = useCallback((updater) => {
    const next = typeof updater === 'function'
      ? updater(pendingMessagesRef.current)
      : updater
    pendingMessagesRef.current = next
    setPendingMessages(next)
  }, [])

  // Resolve a cid out of the deferred-consume guard: evict it from every armed
  // snapshot (so a later same-cid reuse can't false-match a stale snapshot —
  // cids are fresh UUIDs in practice, but this keeps the guard exact), drop any
  // now-empty armed ts, and clear the whole guard once nothing is in flight (no
  // pending swap can then match it). Call after every path that removes a cid
  // from inFlightCidsRef.
  const resolveCidFromGuard = useCallback((cid) => {
    for (const [ts, cids] of consumedServerTsRef.current) {
      if (cids.delete(cid) && cids.size === 0) {
        consumedServerTsRef.current.delete(ts)
      }
    }
    if (inFlightCidsRef.current.size === 0) {
      consumedServerTsRef.current.clear()
    }
  }, [])

  const add = useCallback((msg) => {
    // In-flight protection is ONLY for OPTIMISTIC entries — a local
    // write racing the reconcile read. add() is also used on the
    // fresh-send queued path for an already-server-CONFIRMED entry
    // (cid `s-<ts>`, the server already handed back the ts); that
    // entry's authority is the server list, so it must NOT get
    // hydrate's keep-it protection — otherwise a later normal
    // hydrate that legitimately drops it (the server cleared the
    // queue) would resurrect a phantom row. The `s-` prefix reliably
    // marks server-origin cids; only non-`s-` (optimistic) cids are
    // tracked in-flight.
    const isServerOrigin = msg.cid != null && String(msg.cid).startsWith('s-')
    if (msg.cid != null && !isServerOrigin) {
      inFlightCidsRef.current.add(msg.cid)
    }
    apply(prev => [
      ...prev,
      {
        ...msg,
        position: msg.position ?? prev.length + 1,
        // serverTs marks an entry whose ts is the SERVER's, not an
        // optimistic Date.now(). A server-origin cid (`s-<ts>`, fresh-send
        // queued path) is confirmed by construction; an optimistic add
        // starts unconfirmed and is promoted by swapOptimisticTs once the
        // POST acks. The steer gate reads this — force-steer matches the
        // server's chat.pending_messages[].ts, so an unconfirmed entry
        // cannot be steered and must keep the queue on the Stop path.
        serverTs: msg.serverTs === true || isServerOrigin,
      },
    ])
  }, [apply])

  const swapOptimisticTs = useCallback((cid, serverTs, position, serverMsg, opts = {}) => {
    // The server acknowledged the POST (it handed back a ts): the
    // optimistic round-trip is resolved, so the entry no longer needs
    // hydrate's in-flight protection regardless of which branch runs.
    inFlightCidsRef.current.delete(cid)
    // Remove the entry ONLY when THIS cid was one of the in-flight cids
    // recorded when serverTs was consumed (the guard is cid-scoped). A fresh
    // message that merely got a REISSUED serverTs was not in flight at arm
    // time, so its cid is absent from the set and it is NOT dropped (bug #4).
    const armedCids =
      serverTs != null ? consumedServerTsRef.current.get(serverTs) : undefined
    if (armedCids && armedCids.has(cid)) {
      consumedServerTsRef.current.delete(serverTs)
      apply(prev => prev.filter(m => m.cid !== cid))
      resolveCidFromGuard(cid)
      return
    }
    // A runtime hydrate can land before the optimistic POST ack and already
    // insert the canonical server row (`s-${serverTs}`). When the ack then
    // arrives, mapping the optimistic cid onto that same ts would leave two
    // local chips for one pending row, which makes fast-forward send duplicate
    // content and get rejected by the backend. Prefer the already-hydrated
    // server row and drop the optimistic copy.
    if (serverTs != null && pendingMessagesRef.current.some(
      m => m.cid !== cid && m.ts === serverTs && m.serverTs === true,
    )) {
      apply(prev => prev.filter(m => m.cid !== cid))
      resolveCidFromGuard(cid)
      return
    }
    apply(prev => prev.map(m => {
      if (m.cid !== cid) return m
      // The POST may ack before the server response includes the canonical
      // pending row. When it does include that row, swap the visible item to
      // the server's exact content immediately; force-steer matches against
      // canonical pending text, which can include hidden upload/session
      // context that was not present in the clean composer draft.
      const confirmed = opts.confirmed !== undefined
        ? !!opts.confirmed
        : (serverTs != null || serverMsg?.ts != null || m.serverTs === true)
      const next = {
        ...m,
        ...(serverMsg && typeof serverMsg === 'object' ? serverMsg : {}),
        cid: m.cid,
        queued: true,
        ts: serverTs ?? serverMsg?.ts ?? m.ts,
        serverTs: confirmed,
      }
      if (position !== undefined) next.position = position
      return next
    }))
    resolveCidFromGuard(cid)
  }, [apply, resolveCidFromGuard])

  const promoteByTs = useCallback((ts) => {
    const current = pendingMessagesRef.current
    const idx = ts != null
      ? current.findIndex(m => m.ts === ts)
      : (current.length > 0 ? 0 : -1)
    if (idx < 0) return null
    const promoted = current[idx]
    if (promoted.cid != null) inFlightCidsRef.current.delete(promoted.cid)
    const rest = current.filter((_, i) => i !== idx)
    pendingMessagesRef.current = rest
    setPendingMessages(rest)
    if (promoted.cid != null) resolveCidFromGuard(promoted.cid)
    return promoted
  }, [resolveCidFromGuard])

  const promoteAll = useCallback((ts) => {
    const current = pendingMessagesRef.current
    if (current.length === 0) return null
    const idx = ts != null
      ? current.findIndex(m => m.ts === ts)
      : 0
    if (idx < 0) return null
    const promotedGroup = current.slice(idx)
    const kept = current.slice(0, idx)
    for (const m of promotedGroup) {
      if (m.cid != null) inFlightCidsRef.current.delete(m.cid)
    }
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
      // Single-newline matches backend promotion and fast-forward: multiple
      // queued chats should be separated, not rendered with an extra blank row.
      content: promotedGroup.map(m => m.content || '').filter(Boolean).join('\n'),
      ts: first.ts,
    }
    if (attachments.length > 0) promoted.attachments = attachments
    else delete promoted.attachments
    pendingMessagesRef.current = kept
    setPendingMessages(kept)
    for (const m of promotedGroup) {
      if (m.cid != null) resolveCidFromGuard(m.cid)
    }
    return promoted
  }, [resolveCidFromGuard])

  const promoteManyByTs = useCallback((tsList) => {
    const wanted = new Set((tsList || []).filter(ts => ts != null))
    if (wanted.size === 0) return null
    const current = pendingMessagesRef.current
    // Arm the deferred-removal guard ONLY for a consumed ts whose entry is
    // NOT already in the local list (a present entry is removed directly by ts
    // via the `kept` filter below, so it needs no swap-time removal), AND only
    // while an optimistic POST is in flight (otherwise no swap can ever match
    // it). The guard records the consumed ts → the SNAPSHOT of in-flight cids
    // at this moment: those are the only cids that could legitimately resolve
    // to this consumed ts. swapOptimisticTs removes an entry only when its cid
    // is in that snapshot, so a fresh message later given a REISSUED ts (its
    // cid was not in flight here) is NOT dropped (bug #4).
    if (inFlightCidsRef.current.size > 0) {
      const presentTs = new Set(current.map(m => m.ts))
      for (const ts of wanted) {
        if (!presentTs.has(ts)) {
          // A fresh Set per ts (not a shared reference) so resolveCidFromGuard
          // can prune one armed ts without aliasing into another.
          consumedServerTsRef.current.set(ts, new Set(inFlightCidsRef.current))
        }
      }
    }
    const promotedGroup = current.filter(m => wanted.has(m.ts))
    if (promotedGroup.length === 0) return null
    const kept = current.filter(m => !wanted.has(m.ts))
    for (const m of promotedGroup) {
      if (m.cid != null) inFlightCidsRef.current.delete(m.cid)
    }
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
      // Single-newline matches handleStop, fast-forward, and promoteAll above.
      content: promotedGroup.map(m => m.content || '').filter(Boolean).join('\n'),
      ts: first.ts,
    }
    if (attachments.length > 0) promoted.attachments = attachments
    else delete promoted.attachments
    pendingMessagesRef.current = kept
    setPendingMessages(kept)
    // Resolve the just-promoted (present) cids out of the guard. This is also
    // the ARM path: it may have armed the guard for a DIFFERENT absent ts whose
    // optimistic cid is still in flight, so resolveCidFromGuard won't clear the
    // whole guard (in-flight non-empty) — it only prunes the promoted cids.
    for (const m of promotedGroup) {
      if (m.cid != null) resolveCidFromGuard(m.cid)
    }
    return promoted
  }, [resolveCidFromGuard])

  const cancelByTs = useCallback((ts) => {
    // A cancelled entry is genuinely gone; drop its in-flight mark so
    // it cannot resurrect on the next hydrate.
    const cancelledCids = []
    for (const m of pendingMessagesRef.current) {
      if (m.ts === ts && m.cid != null) {
        inFlightCidsRef.current.delete(m.cid)
        cancelledCids.push(m.cid)
      }
    }
    apply(prev => prev.filter(m => m.ts !== ts))
    for (const cid of cancelledCids) resolveCidFromGuard(cid)
  }, [apply, resolveCidFromGuard])

  const cancelByCid = useCallback((cid) => {
    inFlightCidsRef.current.delete(cid)
    apply(prev => prev.filter(m => m.cid !== cid))
    resolveCidFromGuard(cid)
  }, [apply, resolveCidFromGuard])

  // Reconcile the queue against authoritative server state WITHOUT
  // clobbering an optimistic entry whose own POST is still in flight.
  //
  // The server list is authoritative for everything it has SEEN, but
  // an optimistic-only entry (a local cid with no matching server ts)
  // whose persistence POST has not yet committed is invisible to the
  // server precisely because its write is racing this read — it is
  // not "removed", it is "not yet there". Replacing the tray wholesale
  // with the server list would drop it, persist-but-vanish; that is
  // the reconcile-clobber race (e.g. a Stop-timeout resend's queueOnly
  // POST racing onStreamEnd's fetchMessages → hydrate). So we keep any
  // local entry that is (a) still flagged in-flight AND (b) absent from
  // the server list, appending it after the reconciled server entries.
  // Once its POST lands, swapOptimisticTs clears the flag and a later
  // hydrate treats it as ordinary server state. An entry NOT flagged
  // in-flight (server-confirmed, or cancelled via DELETE /pending) is
  // dropped as before — a cancelled message must not resurrect.
  //
  // cid handling is unchanged: reuse the local cid when a server entry
  // shares its ts (keeps QueuedMessages from remounting; R2 in
  // _034-design.md — the swap race), else mint `s-${ts}`.
  const hydrate = useCallback((serverList, opts = {}) => {
    // Deliberately does NOT clear consumedServerTsRef: a hydrate can land
    // WHILE a legitimate in-flight consume is still pending (its optimistic
    // cid in flight, its consumed server ts armed). Clearing the guard here
    // would let that consumed entry resurface as a visible chip when its swap
    // finally lands. The guard is instead pruned per-cid as each round-trip
    // resolves and fully cleared the moment nothing is in flight
    // (resolveCidFromGuard), which is the precise end of any legitimate race.
    const local = pendingMessagesRef.current || []
    const serverRows = serverList || []
    const localByTs = new Map(local.map(m => [m.ts, m.cid]))
    const serverTsSet = new Set(serverRows.map(m => m.ts))

    // POST ack can race with runtime hydrate: the server row can arrive with
    // the real server ts before swapOptimisticTs maps the optimistic cid to it.
    // In the unambiguous 1-local-in-flight ↔ 1-server-row case, adopt the
    // local cid onto the server row and do NOT preserve the optimistic twin.
    // Ambiguous duplicate content (two identical queued messages) is left for
    // the ack path rather than risking a lossy collapse.
    const serverByIdentity = new Map()
    for (const row of serverRows) {
      const key = _queueIdentityKey(row)
      const list = serverByIdentity.get(key) || []
      list.push(row)
      serverByIdentity.set(key, list)
    }
    const localInFlightByIdentity = new Map()
    for (const row of local) {
      if (!row?.cid || !inFlightCidsRef.current.has(row.cid) || serverTsSet.has(row.ts)) continue
      const key = _queueIdentityKey(row)
      const list = localInFlightByIdentity.get(key) || []
      list.push(row)
      localInFlightByIdentity.set(key, list)
    }
    const cidByServerTs = new Map()
    const matchedInFlightCids = new Set()
    for (const [key, serverMatches] of serverByIdentity) {
      const localMatches = localInFlightByIdentity.get(key) || []
      if (serverMatches.length === 1 && localMatches.length === 1) {
        const localMatch = localMatches[0]
        cidByServerTs.set(serverMatches[0].ts, localMatch.cid)
        matchedInFlightCids.add(localMatch.cid)
      }
    }
    for (const cid of matchedInFlightCids) {
      inFlightCidsRef.current.delete(cid)
      resolveCidFromGuard(cid)
    }

    const reconciled = serverRows.map(m => ({
      ...m,
      cid: localByTs.get(m.ts) || cidByServerTs.get(m.ts) || `s-${m.ts}`,
      queued: true,
      // Everything in the server list has a real server ts by definition.
      serverTs: true,
    }))
    const preservedInFlight = local.filter(m =>
      m.cid != null
      && inFlightCidsRef.current.has(m.cid)
      && !matchedInFlightCids.has(m.cid)
      && !serverTsSet.has(m.ts),
    )
    const preservedCidSet = new Set(preservedInFlight.map(m => m.cid))
    const preservedMissing = opts.preserveMissing
      ? local
          .filter(m =>
            m
            && !serverTsSet.has(m.ts)
            && !preservedCidSet.has(m.cid)
          )
          .map(m => ({
            ...m,
            // The server omitted this row during an active-turn/runtime
            // reconcile. Keep the human-visible text instead of dropping it,
            // but downgrade the row so fast-forward cannot force-steer using
            // a server ts the backend no longer confirms. A later hydrate
            // that includes the row will re-confirm serverTs=true.
            serverTs: false,
            missingFromServer: true,
          }))
      : []
    const next = [...reconciled, ...preservedInFlight, ...preservedMissing]
    pendingMessagesRef.current = next
    setPendingMessages(next)
  }, [resolveCidFromGuard])

  const clear = useCallback(() => {
    consumedServerTsRef.current.clear()
    inFlightCidsRef.current.clear()
    pendingMessagesRef.current = []
    setPendingMessages([])
  }, [])

  // Explicit in-flight controls. add() already marks an optimistic
  // entry in-flight and the standard resolve paths clear it, so most
  // callers never touch these. clearInFlight has one production caller:
  // doSend's queue-path fallthrough, which resolves the flag for a
  // terminal streamSend status (e.g. `not_steered`) that no swap or
  // cancel covers. markInFlight remains available for a caller that
  // adds an entry through a non-standard path.
  const markInFlight = useCallback((cid) => {
    if (cid != null) inFlightCidsRef.current.add(cid)
  }, [])

  const clearInFlight = useCallback((cid) => {
    inFlightCidsRef.current.delete(cid)
    resolveCidFromGuard(cid)
  }, [resolveCidFromGuard])

  return {
    pendingMessages,
    pendingMessagesRef,
    add,
    swapOptimisticTs,
    promoteByTs,
    promoteAll,
    promoteManyByTs,
    cancelByTs,
    cancelByCid,
    hydrate,
    clear,
    markInFlight,
    clearInFlight,
  }
}
