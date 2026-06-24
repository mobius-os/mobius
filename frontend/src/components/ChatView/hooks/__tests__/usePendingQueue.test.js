/**
 * Unit tests for usePendingQueue.
 *
 * Run with:
 *   cd frontend && node --loader=./src/components/ChatView/hooks/__tests__/react-loader.mjs \
 *     --test src/components/ChatView/hooks/__tests__/usePendingQueue.test.js
 *
 * The loader aliases `react` -> react-hook-shim so the hook can be
 * driven from node without a renderer. See react-hook-shim.mjs.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import usePendingQueue from '../usePendingQueue.js'

function fixtureMsg(overrides = {}) {
  return {
    role: 'user',
    content: 'hi',
    ts: 100,
    cid: 'cid-1',
    queued: true,
    ...overrides,
  }
}

test('add appends and the ref updates synchronously', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg())
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'cid-1')
  assert.equal(result.current.pendingMessagesRef.current[0].position, 1)
  result.current.add(fixtureMsg({ cid: 'cid-2', ts: 200 }))
  assert.equal(result.current.pendingMessagesRef.current.length, 2)
  assert.equal(result.current.pendingMessagesRef.current[1].position, 2)
})

test('swapOptimisticTs preserves cid while updating ts and position', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 999 }))
  result.current.swapOptimisticTs('optimistic-x', 12345, 3)
  const list = result.current.pendingMessagesRef.current
  assert.equal(list[0].cid, 'optimistic-x', 'cid must persist across ts swap')
  assert.equal(list[0].ts, 12345)
  assert.equal(list[0].position, 3)
})

test('promoteByTs returns the matching entry and removes it', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1 }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2 }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3 }))
  const got = result.current.promoteByTs(2)
  assert.equal(got.cid, 'b')
  assert.equal(result.current.pendingMessagesRef.current.length, 2)
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['a', 'c'],
  )
})

test('promoteByTs returns null when no match', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ ts: 1 }))
  assert.equal(result.current.promoteByTs(99), null)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
})

test('promoteAll collapses the matching entry and everything after it', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  const got = result.current.promoteAll(2)
  assert.equal(got.cid, 'b')
  assert.equal(got.ts, 2)
  // Double-newline join matches handleStop and renders as separate paragraphs.
  assert.equal(got.content, 'second\n\nthird')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['a'],
  )
})

test('promoteAll only consumes the queue present at promotion time', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  const got = result.current.promoteAll(1)
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  assert.equal(got.content, 'first\n\nsecond')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['c'],
  )
})

test('promoteManyByTs preserves later entries not consumed by the backend', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  const got = result.current.promoteManyByTs([1, 2])
  // Double-newline join matches handleStop and promoteAll.
  assert.equal(got.content, 'first\n\nsecond')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['c'],
  )
})

test('swapOptimisticTs removes a chip whose server ts was already consumed', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-a', ts: 999, content: 'first' }))
  assert.equal(result.current.promoteManyByTs([10]), null)
  result.current.swapOptimisticTs('optimistic-a', 10, 1)
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
})

test('a REISSUED server ts does not drop a fresh queued message (bug #4)', () => {
  // Bug #4: the consumed-ts guard used to accumulate forever. promoteManyByTs
  // armed every consumed ts (including ones for entries already removable by
  // ts), and the set was never drained. Later, _ensure_unique_ts REISSUES a
  // freed ts to a NEW queued message (the row holding it was deleted). The
  // new message's swapOptimisticTs then matched the stale consumed ts and
  // DROPPED it — "a message didn't show up as queued". The fix arms the guard
  // only for a consumed ts ABSENT from the list (a genuine in-flight race),
  // so a normal promote-by-ts of present entries leaves the set empty.
  const { result } = renderHook(usePendingQueue)
  // Two confirmed entries get combined + started. The backend's _consumed_ts
  // carries BOTH ts (10 and 11); both rows are present in the list, so the
  // promote removes them by ts — and must NOT arm the guard for either.
  result.current.add(fixtureMsg({ cid: 's-10', ts: 10, serverTs: true, content: 'a' }))
  result.current.add(fixtureMsg({ cid: 's-11', ts: 11, serverTs: true, content: 'b' }))
  result.current.promoteManyByTs([10, 11])
  assert.deepEqual(result.current.pendingMessagesRef.current, [],
    'both started entries are consumed')

  // Later the user queues a fresh message; the server REISSUES ts 11 (freed
  // when the old row was promoted). Its POST acks via swapOptimisticTs.
  result.current.add(fixtureMsg({ cid: 'fresh', ts: 99999, content: 'new one' }))
  result.current.swapOptimisticTs('fresh', 11, 1)

  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1, 'the fresh queued message must NOT be dropped')
  assert.equal(list[0].cid, 'fresh')
  assert.equal(list[0].ts, 11, 'it takes the reissued server ts')
  assert.equal(list[0].content, 'new one')
})

test('promoteManyByTs does not arm the guard when nothing is in flight', () => {
  // The guard exists only for an OPTIMISTIC entry still in flight whose ts was
  // consumed before its swap. With nothing in flight, arming would leave a
  // stale ts that a server-reissued ts later collides with. So a promote with
  // an empty in-flight set must arm nothing, and a fresh message later given
  // that ts must survive.
  const { result } = renderHook(usePendingQueue)
  result.current.promoteManyByTs([42])  // nothing in flight → arms nothing
  result.current.add(fixtureMsg({ cid: 'fresh2', ts: 88888, content: 'survivor' }))
  result.current.swapOptimisticTs('fresh2', 42, 1)
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1, 'no stale guard armed; the reissue is safe')
  assert.equal(list[0].cid, 'fresh2')
  assert.equal(list[0].ts, 42)
})

test('a legitimate in-flight consume survives a hydrate landing mid-race', () => {
  // The guard MUST hold across a hydrate that lands while the consume is still
  // in flight (its optimistic cid unresolved). hydrate must not clear the
  // guard — otherwise the consumed entry resurfaces as a visible chip when its
  // swap lands. Sequence: optimistic add (in flight) → its server ts gets
  // consumed by a started turn (promoteManyByTs arms it because the entry is
  // present with a DIFFERENT client ts) → a hydrate lands mid-race → the swap
  // finally acks with the consumed server ts and must REMOVE the entry.
  const { result } = renderHook(usePendingQueue)
  // Optimistic entry: client ts 555, in flight (cid 'opt', not s-).
  result.current.add(fixtureMsg({ cid: 'opt', ts: 555, content: 'consumed soon' }))
  // The started turn consumes its SERVER ts (200) — absent locally (the entry
  // still shows client ts 555), and a cid is in flight, so the guard arms 200.
  result.current.promoteManyByTs([200])
  // A reconcile lands mid-race; the in-flight entry is preserved, the guard
  // must NOT be cleared.
  result.current.hydrate([])
  assert.equal(result.current.pendingMessagesRef.current.length, 1,
    'the in-flight entry is preserved across the mid-race hydrate')
  // The POST finally acks with the consumed server ts → the entry is removed
  // (it was consumed into the started turn), not left as a phantom chip.
  result.current.swapOptimisticTs('opt', 200, 1)
  assert.deepEqual(result.current.pendingMessagesRef.current, [],
    'the consumed entry is removed on swap; it does not resurface')
})

test('a reissued ts does not drop a fresh message while an UNRELATED entry is in flight', () => {
  // The cid-scoped guard (Map<ts, Set<cid>>) closes the residual hole a bare
  // ts-set left: a guard armed for an absent consumed ts while some UNRELATED
  // optimistic entry is in flight must not drop a DIFFERENT fresh message that
  // later gets that ts reissued. Only the cids in flight AT ARM TIME may be
  // removed by the guard.
  const { result } = renderHook(usePendingQueue)
  // An unrelated optimistic entry C is in flight (its POST hasn't acked).
  result.current.add(fixtureMsg({ cid: 'C', ts: 700, content: 'unrelated' }))
  // A started turn consumes ts 11 — absent locally (no entry has ts 11), and a
  // cid (C) is in flight, so the guard arms 11 with snapshot {C}.
  result.current.promoteManyByTs([11])
  // A fresh message D is queued and the server REISSUES ts 11 to it. D's cid
  // was NOT in flight when 11 was armed, so the guard must NOT drop it.
  result.current.add(fixtureMsg({ cid: 'D', ts: 88, content: 'fresh D' }))
  result.current.swapOptimisticTs('D', 11, 2)
  const list = result.current.pendingMessagesRef.current
  const d = list.find(m => m.cid === 'D')
  assert.ok(d, 'the fresh message D must survive (cid not in the armed snapshot)')
  assert.equal(d.ts, 11, 'D takes the reissued server ts')
  // C is still in flight and untouched.
  assert.ok(list.find(m => m.cid === 'C'), 'the unrelated in-flight entry is untouched')
})

test('cancelling a cid evicts it from armed guard snapshots (no stale false-drop)', () => {
  // Defense in depth: a cid removed from flight via cancel must also be evicted
  // from any armed guard snapshot, so a later reuse of the SAME cid (cids are
  // fresh UUIDs in practice, so this is belt-and-suspenders) can't false-match
  // the stale snapshot and drop a fresh message. Sequence: 'X' in flight →
  // guard armed for absent ts 9 with snapshot {X} → X cancelled → X re-added →
  // swap X→9 must NOT drop it (the stale {X} snapshot was evicted on cancel).
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'X', ts: 300, content: 'first life' }))
  result.current.promoteManyByTs([9])   // arms guard[9] = {X}
  result.current.cancelByCid('X')        // X leaves flight → evict from guard[9]
  result.current.add(fixtureMsg({ cid: 'X', ts: 301, content: 'reused cid' }))
  result.current.swapOptimisticTs('X', 9, 1)
  const list = result.current.pendingMessagesRef.current
  const x = list.find(m => m.cid === 'X')
  assert.ok(x, 'the re-added X must survive (stale guard snapshot was evicted)')
  assert.equal(x.ts, 9, 'X takes the server ts via the normal promote path')
})

test('promoteAll returns null when no matching anchor exists', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ ts: 1 }))
  assert.equal(result.current.promoteAll(99), null)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
})

test('cancelByTs removes by ts', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1 }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2 }))
  result.current.cancelByTs(1)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'b')
})

test('cancelByCid removes the matching entry (pre-swap rollback path)', () => {
  // Used in doSend's error rollback and the "server said started"
  // branch — both run before the optimistic ts has been swapped to
  // the server ts, so cid is the only stable handle.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 99 }))
  result.current.add(fixtureMsg({ cid: 'optimistic-y', ts: 100 }))
  result.current.cancelByCid('optimistic-x')
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'optimistic-y')
})

test('hydrate replaces a RESOLVED local entry with server state; ref updates synchronously', () => {
  // hydrate is a merge, not a wholesale replace, but it only PRESERVES
  // entries whose POST is still in flight (see the in-flight tests
  // below). A local entry whose round-trip already resolved — here,
  // swapped to a server ts the server no longer lists — is authoritative
  // server state's to drop. swapOptimisticTs clears the in-flight flag,
  // so this `local` entry is not preserved and the server list wins.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'local', ts: 1 }))
  result.current.swapOptimisticTs('local', 1)
  result.current.hydrate([
    { role: 'user', content: 'srv', ts: 50 },
    { role: 'user', content: 'srv2', ts: 60 },
  ])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 2)
  assert.equal(list[0].cid, 's-50')
  assert.equal(list[1].cid, 's-60')
  assert.equal(list[0].queued, true)
})

test('hydrate preserves the local cid when server ts matches an existing entry', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 999 }))
  result.current.swapOptimisticTs('optimistic-x', 12345)
  result.current.hydrate([{ role: 'user', content: 'hi', ts: 12345 }])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1)
  assert.equal(list[0].cid, 'optimistic-x',
    'hydrate must reuse the local cid when ts matches')
})

test('swapOptimisticTs racing with hydrate keeps cid stability', () => {
  // R2 from _034-design.md: a hydrate (slow refetch) landing
  // interleaved with an optimistic add+swap must not flip the cid
  // out from under QueuedMessages. Sequence: add optimistic, swap
  // its ts to the value the server is about to claim, then a
  // hydrate carrying that server ts arrives — cid must stay
  // 'optimistic-x', not become 's-12345'.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 999 }))
  // Server replied with the canonical ts; we swapped locally.
  result.current.swapOptimisticTs('optimistic-x', 12345)
  // Concurrent hydrate from a refetch that started before our swap
  // landed but resolved after it — server's view also has ts=12345.
  result.current.hydrate([{ role: 'user', content: 'hi', ts: 12345 }])
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'optimistic-x')
})

test('clear resets state and ref synchronously', () => {
  // R1 from _034-design.md: handleStop calls clear() then reads
  // pendingMessagesRef.current synchronously to decide whether to
  // skip an in-flight fetchMessages. If clear only scheduled a
  // render, the ref read would still see the pre-stop queue.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ ts: 1 }))
  result.current.add(fixtureMsg({ ts: 2 }))
  result.current.clear()
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
})

test('stop-timeout: restoring the local snapshot preserves the queue that a server refetch would drop', () => {
  // Regression lock-in for the Stop queued-message-drop bug.
  //
  // handleStop snapshots the queue, clear()s it, then POSTs /chat/stop.
  // On an SDK interrupt TIMEOUT (stopped===false) the backend has
  // ALREADY cleared persisted chat.pending_messages — so the old code's
  // recovery (refetch /chats/<id> → hydrate(data.pending_messages))
  // hydrated [] and the queued text vanished. The fix restores from the
  // LOCAL snapshot (via doSend's queue path, which re-adds to the tray
  // and re-persists server-side) instead of trusting the now-empty
  // server view. This test pins the hook-level invariant the fix leans
  // on: after a Stop-clear, hydrating the empty server response WIPES the
  // queue, while re-adding the snapshot KEEPS it.
  const { result } = renderHook(usePendingQueue)
  const snapshot = [
    fixtureMsg({ cid: 'q1', ts: 11, content: 'first queued' }),
    fixtureMsg({ cid: 'q2', ts: 22, content: 'second queued' }),
  ]
  for (const m of snapshot) result.current.add(m)
  // Stop's synchronous pre-await clear.
  result.current.clear()
  assert.deepEqual(result.current.pendingMessagesRef.current, [])

  // What the OLD recovery did on a stop timeout: hydrate the server's
  // (now empty) pending → the queued text is lost.
  result.current.hydrate([])
  assert.deepEqual(
    result.current.pendingMessagesRef.current, [],
    'hydrating the empty server response on a stop timeout drops the queue',
  )

  // What the FIX does: restore the local snapshot so the queued text
  // survives and reappears in the tray.
  for (const m of snapshot) result.current.add(m)
  const restored = result.current.pendingMessagesRef.current
  assert.equal(restored.length, 2, 'both queued messages are restored')
  assert.equal(restored[0].content, 'first queued')
  assert.equal(restored[1].content, 'second queued')
})

test('hydrate([]) PRESERVES an optimistic entry whose POST is still in flight', () => {
  // The structural root-cause fix (audit HIGH, fix B). A reconcile-fetch
  // that lands while an optimistic entry's persistence POST is still in
  // flight must NOT drop it: the server simply hasn't seen the write yet
  // (it is racing this read), so an empty server list does not mean the
  // entry was removed. This is the exact ordering of the Stop-timeout
  // resend racing onStreamEnd's fetchMessages({force:true})→hydrate([]).
  const { result } = renderHook(usePendingQueue)
  // doSend's queue path: add() marks the cid in-flight; the await on the
  // POST has not resolved (no swapOptimisticTs yet).
  result.current.add(fixtureMsg({ cid: 'inflight-1', ts: 777, content: 're-queued combined' }))
  // onStreamEnd's continues:false refetch reconciles against a server
  // list that does NOT yet contain the racing write.
  result.current.hydrate([])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1, 'the in-flight optimistic entry survives hydrate([])')
  assert.equal(list[0].cid, 'inflight-1')
  assert.equal(list[0].content, 're-queued combined')
})

test('a server-CONFIRMED `s-<ts>` add is DROPPED by a later hydrate([])', () => {
  // Convergent re-audit regression. The fresh-send queued path
  // (ChatView ~1131) add()s an ALREADY-server-confirmed entry with cid
  // `s-<ts>` (the server handed back the ts) and, when result.started
  // is false, returns WITHOUT swapOptimisticTs/clearInFlight. If add()
  // marked every cid in-flight, this server row would survive a later
  // normal hydrate([]) — e.g. the continues:false error-clear refetch
  // (ChatView ~488-493) — leaving a phantom queued message in the tray
  // that the authoritative server list intentionally removed. In-flight
  // protection is OPTIMISTIC-only: the `s-` prefix marks server-origin
  // cids, so they are NOT preserved and hydrate is free to drop them.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 's-123', ts: 123, content: 'server-confirmed' }))
  result.current.hydrate([])
  assert.deepEqual(
    result.current.pendingMessagesRef.current, [],
    'a server-confirmed s-<ts> entry the server omits is dropped by hydrate, not resurrected',
  )
})

test('an in-flight entry survives hydrate regardless of POST-vs-fetch ordering', () => {
  // The race is symmetric — the fix must hold whichever lands first.
  //
  // Ordering A: fetch (hydrate) wins, THEN the POST commits.
  const a = renderHook(usePendingQueue)
  a.result.current.add(fixtureMsg({ cid: 'race-a', ts: 500, content: 'combined' }))
  a.result.current.hydrate([])                       // refetch wins first
  assert.equal(a.result.current.pendingMessagesRef.current.length, 1,
    'entry survives the reconcile that ran before the POST committed')
  a.result.current.swapOptimisticTs('race-a', 9001)  // POST then commits
  const afterA = a.result.current.pendingMessagesRef.current
  assert.equal(afterA.length, 1)
  assert.equal(afterA[0].cid, 'race-a', 'cid stays stable through the swap')
  assert.equal(afterA[0].ts, 9001, 'server ts promoted in')

  // Ordering B: the POST commits FIRST (entry now has a server ts and is
  // no longer in-flight), THEN a reconcile carrying that ts arrives.
  const b = renderHook(usePendingQueue)
  b.result.current.add(fixtureMsg({ cid: 'race-b', ts: 600, content: 'combined' }))
  b.result.current.swapOptimisticTs('race-b', 9002)  // POST commits first
  b.result.current.hydrate([{ role: 'user', content: 'combined', ts: 9002 }])
  const afterB = b.result.current.pendingMessagesRef.current
  assert.equal(afterB.length, 1, 'no duplicate when the server now lists the entry')
  assert.equal(afterB[0].cid, 'race-b', 'reconcile reuses the local cid by matching ts')
})

test('swapOptimisticTs clears in-flight so a later hydrate treats the entry as server state', () => {
  // Once the POST commits the entry is no longer in-flight; if the server
  // later DROPS it (e.g. it was consumed into a turn), a reconcile must be
  // free to remove it — preservation is only for the racing-write window.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'committed', ts: 700 }))
  result.current.swapOptimisticTs('committed', 8001)   // POST committed
  result.current.hydrate([])                            // server no longer lists it
  assert.deepEqual(result.current.pendingMessagesRef.current, [],
    'a committed (not in-flight) entry the server omits is dropped by hydrate')
})

test('a cancelled entry does NOT resurrect on a later hydrate', () => {
  // DELETE /pending → cancelByCid/cancelByTs removes the entry AND clears
  // its in-flight mark, so a subsequent reconcile must not bring it back.
  const byCid = renderHook(usePendingQueue)
  byCid.result.current.add(fixtureMsg({ cid: 'cancel-cid', ts: 800 }))
  byCid.result.current.cancelByCid('cancel-cid')
  byCid.result.current.hydrate([])
  assert.deepEqual(byCid.result.current.pendingMessagesRef.current, [],
    'cancelByCid entry stays gone across hydrate')

  const byTs = renderHook(usePendingQueue)
  byTs.result.current.add(fixtureMsg({ cid: 'cancel-ts', ts: 801 }))
  byTs.result.current.cancelByTs(801)
  byTs.result.current.hydrate([])
  assert.deepEqual(byTs.result.current.pendingMessagesRef.current, [],
    'cancelByTs entry stays gone across hydrate')
})

test('serverTs gate: optimistic add starts unconfirmed; swap/hydrate/s-cid confirm it', () => {
  // The steer (fast-forward) feature only converts SERVER-confirmed queued
  // messages — force_steer matches against chat.pending_messages[].ts, so an
  // optimistic Date.now() ts would not match. usePendingQueue stamps
  // `serverTs` true on exactly the paths that produce a real server ts:
  // a server-origin `s-` add, a swapOptimisticTs ack, or a hydrate.
  const { result } = renderHook(usePendingQueue)

  // Optimistic add: NOT yet server-confirmed (ts is a client value).
  result.current.add(fixtureMsg({ cid: 'opt-1', ts: 111 }))
  assert.equal(result.current.pendingMessagesRef.current[0].serverTs, false,
    'an optimistic add is not server-confirmed until its POST acks')

  // The POST acks → swapOptimisticTs promotes the server ts + confirms.
  result.current.swapOptimisticTs('opt-1', 222)
  assert.equal(result.current.pendingMessagesRef.current[0].serverTs, true,
    'swapOptimisticTs confirms the entry')

  // Fresh-send queued path add()s an already-confirmed `s-<ts>` entry.
  result.current.add(fixtureMsg({ cid: 's-333', ts: 333 }))
  assert.equal(
    result.current.pendingMessagesRef.current.find(m => m.cid === 's-333').serverTs,
    true, 'a server-origin s-<ts> add is confirmed by construction')

  // Hydrate yields server state — every reconciled entry is confirmed.
  result.current.hydrate([{ role: 'user', content: 'srv', ts: 444 }])
  assert.equal(result.current.pendingMessagesRef.current[0].serverTs, true,
    'hydrated entries are server-confirmed')
})

test('a promoted entry does NOT resurrect on a later hydrate', () => {
  // Promotion consumes the entry into the active turn and clears its
  // in-flight mark; a reconcile must not re-add it to the tray.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'promote-me', ts: 900 }))
  result.current.add(fixtureMsg({ cid: 'keep-me', ts: 901 }))
  result.current.promoteManyByTs([900])
  result.current.hydrate([])
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['keep-me'],
    'the promoted entry is not resurrected; the still-in-flight sibling survives',
  )
})
