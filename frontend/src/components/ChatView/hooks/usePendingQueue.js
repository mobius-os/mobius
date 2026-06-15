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
 *   swapOptimisticTs: (cid: string, serverTs: number, position?: number) => void,
 *   promoteByTs: (ts: number) => PendingMsg | null,
 *   promoteAll: (ts: number) => PendingMsg | null,
 *   promoteManyByTs: (tsList: number[]) => PendingMsg | null,
 *   cancelByTs: (ts: number) => void,
 *   cancelByCid: (cid: string) => void,
 *   hydrate: (serverList: Array<{ts: number, content: string, role?: string, attachments?: Array, position?: number}>) => void, // MERGE: server list + any in-flight optimistic-only entry the server hasn't seen yet
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
export default function usePendingQueue() {
  const [pendingMessages, setPendingMessages] = useState([])
  const pendingMessagesRef = useRef([])
  const consumedServerTsRef = useRef(new Set())
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

  const add = useCallback((msg) => {
    // The optimistic add is the start of the persistence round-trip:
    // the POST is in flight until swapOptimisticTs / cancel / promote
    // resolves it. Mark the cid so a concurrent hydrate keeps it.
    if (msg.cid != null) inFlightCidsRef.current.add(msg.cid)
    apply(prev => [
      ...prev,
      { ...msg, position: msg.position ?? prev.length + 1 },
    ])
  }, [apply])

  const swapOptimisticTs = useCallback((cid, serverTs, position) => {
    // The server acknowledged the POST (it handed back a ts): the
    // optimistic round-trip is resolved, so the entry no longer needs
    // hydrate's in-flight protection regardless of which branch runs.
    inFlightCidsRef.current.delete(cid)
    if (serverTs != null && consumedServerTsRef.current.has(serverTs)) {
      consumedServerTsRef.current.delete(serverTs)
      apply(prev => prev.filter(m => m.cid !== cid))
      return
    }
    apply(prev => prev.map(m => {
      if (m.cid !== cid) return m
      const next = { ...m, ts: serverTs ?? m.ts }
      if (position !== undefined) next.position = position
      return next
    }))
  }, [apply])

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
    return promoted
  }, [])

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
      // Double-newline matches handleStop's join so promoted multi-message
      // blocks render as separate paragraphs in the markdown renderer.
      content: promotedGroup.map(m => m.content || '').filter(Boolean).join('\n\n'),
      ts: first.ts,
    }
    if (attachments.length > 0) promoted.attachments = attachments
    else delete promoted.attachments
    pendingMessagesRef.current = kept
    setPendingMessages(kept)
    return promoted
  }, [])

  const promoteManyByTs = useCallback((tsList) => {
    const wanted = new Set((tsList || []).filter(ts => ts != null))
    if (wanted.size === 0) return null
    for (const ts of wanted) consumedServerTsRef.current.add(ts)
    const current = pendingMessagesRef.current
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
      // Double-newline matches handleStop's join and promoteAll above.
      content: promotedGroup.map(m => m.content || '').filter(Boolean).join('\n\n'),
      ts: first.ts,
    }
    if (attachments.length > 0) promoted.attachments = attachments
    else delete promoted.attachments
    pendingMessagesRef.current = kept
    setPendingMessages(kept)
    return promoted
  }, [])

  const cancelByTs = useCallback((ts) => {
    // A cancelled entry is genuinely gone; drop its in-flight mark so
    // it cannot resurrect on the next hydrate.
    for (const m of pendingMessagesRef.current) {
      if (m.ts === ts && m.cid != null) inFlightCidsRef.current.delete(m.cid)
    }
    apply(prev => prev.filter(m => m.ts !== ts))
  }, [apply])

  const cancelByCid = useCallback((cid) => {
    inFlightCidsRef.current.delete(cid)
    apply(prev => prev.filter(m => m.cid !== cid))
  }, [apply])

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
  const hydrate = useCallback((serverList) => {
    const localByTs = new Map(
      (pendingMessagesRef.current || []).map(m => [m.ts, m.cid])
    )
    const serverTsSet = new Set((serverList || []).map(m => m.ts))
    const reconciled = (serverList || []).map(m => ({
      ...m,
      cid: localByTs.get(m.ts) || `s-${m.ts}`,
      queued: true,
    }))
    const preserved = (pendingMessagesRef.current || []).filter(m =>
      m.cid != null
      && inFlightCidsRef.current.has(m.cid)
      && !serverTsSet.has(m.ts),
    )
    const next = [...reconciled, ...preserved]
    pendingMessagesRef.current = next
    setPendingMessages(next)
  }, [])

  const clear = useCallback(() => {
    consumedServerTsRef.current.clear()
    inFlightCidsRef.current.clear()
    pendingMessagesRef.current = []
    setPendingMessages([])
  }, [])

  // Explicit in-flight controls. add() already marks an optimistic
  // entry in-flight and the resolve paths clear it, so callers rarely
  // need these — they exist so a caller that adds an entry through a
  // non-standard path (or wants to assert the contract) can manage the
  // flag directly.
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
