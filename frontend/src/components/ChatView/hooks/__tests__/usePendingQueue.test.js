/**
 * Unit tests for usePendingQueue (cid-identity model).
 *
 * Run with:
 *   cd frontend && node --loader=./src/components/ChatView/hooks/__tests__/react-loader.mjs \
 *     --test src/components/ChatView/hooks/__tests__/usePendingQueue.test.js
 *
 * The loader aliases `react` -> react-hook-shim so the hook can be
 * driven from node without a renderer. See react-hook-shim.mjs.
 *
 * Identity is the client-minted `cid`; it never changes across the
 * optimistic→confirm display-ts update. There is no swap, no reissued-ts
 * guard, and no content-identity hydrate heuristic — hydrate/confirm/promote
 * all key on cid.
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

test('_fromServerList carries the server row\'s explicit cid', () => {
  // Post-card-221 every server row carries an explicit cid (client-minted, or a
  // backfilled `legacy-<ts>`); _fromServerList carries it through unchanged.
  const { result } = renderHook(usePendingQueue, [
    { role: 'user', content: 'legacy', ts: 42, cid: 'legacy-42' },
  ])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1)
  assert.equal(list[0].cid, 'legacy-42')
  assert.equal(list[0].serverTs, true)
})

test('confirmQueued preserves cid while updating ts and position (no remount)', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'stable-x', ts: 999 }), { inFlight: true })
  result.current.confirmQueued('stable-x', { ts: 12345, position: 3 })
  const list = result.current.pendingMessagesRef.current
  assert.equal(list[0].cid, 'stable-x', 'cid is the stable identity across confirm')
  assert.equal(list[0].ts, 12345, 'ts is display-only and updates in place')
  assert.equal(list[0].position, 3)
  assert.equal(list[0].serverTs, true)
})

test('confirmQueued can replace optimistic content with the server canonical row', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'stable-x', ts: 999, content: 'clean draft' }), { inFlight: true })
  result.current.confirmQueued('stable-x', {
    ts: 12345,
    position: 2,
    serverMsg: {
      role: 'user',
      ts: 12345,
      cid: 'stable-x',
      content: 'clean draft\n\n[Files in this session:\n- Screenshot.png → /x.png\n]',
      attachments: [{ name: 'Screenshot.png' }],
    },
  })
  const list = result.current.pendingMessagesRef.current
  assert.equal(list[0].cid, 'stable-x', 'client cid stays stable for row UI')
  assert.equal(list[0].ts, 12345)
  assert.equal(list[0].content.includes('[Files in this session:'), true,
    'canonical server content is retained for force-steer matching')
  assert.equal(list[0].attachments[0].name, 'Screenshot.png')
  assert.equal(list[0].serverTs, true)
  assert.equal(list[0].position, 2)
})

test('promoteByCid returns the matching entry and removes it', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1 }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2 }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3 }))
  const got = result.current.promoteByCid('b')
  assert.equal(got.cid, 'b')
  assert.equal(result.current.pendingMessagesRef.current.length, 2)
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['a', 'c'],
  )
})

test('promoteByCid returns null when no match', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1 }))
  assert.equal(result.current.promoteByCid('nope'), null)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
})

test('promoteAll collapses the matching cid and everything after it', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  const got = result.current.promoteAll('b')
  assert.equal(got.cid, 'b')
  assert.equal(got.ts, 2)
  // Single-newline join matches backend promotion and avoids an extra blank row.
  assert.equal(got.content, 'second\nthird')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['a'],
  )
})

test('promoteAll() with no arg collapses the whole queue from the head', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  const got = result.current.promoteAll()
  assert.equal(got.content, 'first\nsecond')
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
})

test('promoteManyByCid preserves later entries not consumed by the backend', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  const got = result.current.promoteManyByCid(['a', 'b'])
  assert.equal(got.content, 'first\nsecond')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['c'],
  )
})

test('promoteAll returns null when no matching anchor cid exists', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1 }))
  assert.equal(result.current.promoteAll('missing'), null)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
})

test('cancelByCid removes the matching entry', () => {
  // Used in doSend's error rollback, the "server said started" branch, and the
  // X-in-tray cancel — cid is the one stable handle across every path.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'x', ts: 99 }))
  result.current.add(fixtureMsg({ cid: 'y', ts: 100 }))
  result.current.cancelByCid('x')
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'y')
})

test('cancelByCid removes a row by its explicit (backfilled legacy) cid', () => {
  const { result } = renderHook(usePendingQueue, [
    { role: 'user', content: 'legacy', ts: 55, cid: 'legacy-55' },
  ])
  result.current.cancelByCid('legacy-55')
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
})

test('hydrate replaces a RESOLVED local entry with server state; ref updates synchronously', () => {
  // hydrate is a merge, not a wholesale replace, but it only PRESERVES entries
  // whose POST is still in flight. A resolved local entry (confirmed) the
  // server no longer lists is authoritative server state's to drop.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'local', ts: 1 }), { inFlight: true })
  result.current.confirmQueued('local', { ts: 1 })
  result.current.hydrate([
    { role: 'user', content: 'srv', ts: 50, cid: 's50' },
    { role: 'user', content: 'srv2', ts: 60, cid: 's60' },
  ])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 2)
  assert.equal(list[0].cid, 's50')
  assert.equal(list[1].cid, 's60')
  assert.equal(list[0].queued, true)
})

test('hydrate reuses the local row identity when the server row shares its cid', () => {
  // The server echoes the same cid the client minted, so hydrate matches by cid
  // and the row keeps its stable identity (QueuedMessages keeps its UI state).
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'stable-x', ts: 999 }), { inFlight: true })
  result.current.confirmQueued('stable-x', { ts: 12345 })
  result.current.hydrate([{ role: 'user', content: 'hi', ts: 12345, cid: 'stable-x' }])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1)
  assert.equal(list[0].cid, 'stable-x', 'hydrate keeps the shared cid')
  assert.equal(list[0].ts, 12345)
})

test('clear resets state and ref synchronously', () => {
  // handleStop calls clear() then reads pendingMessagesRef.current
  // synchronously to decide whether to skip an in-flight fetchMessages.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ ts: 1 }))
  result.current.add(fixtureMsg({ cid: 'cid-2', ts: 2 }))
  result.current.clear()
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
})

test('stop-timeout: restoring the local snapshot preserves the queue a refetch would drop', () => {
  // handleStop snapshots the queue, clear()s it, then POSTs /chat/stop. On an
  // SDK interrupt TIMEOUT the backend has already cleared persisted pending, so
  // hydrating the empty server response WIPES the queue while re-adding the
  // snapshot KEEPS it.
  const { result } = renderHook(usePendingQueue)
  const snapshot = [
    fixtureMsg({ cid: 'q1', ts: 11, content: 'first queued' }),
    fixtureMsg({ cid: 'q2', ts: 22, content: 'second queued' }),
  ]
  for (const m of snapshot) result.current.add(m)
  result.current.clear()
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
  result.current.hydrate([])
  assert.deepEqual(result.current.pendingMessagesRef.current, [],
    'hydrating the empty server response on a stop timeout drops the queue')
  for (const m of snapshot) result.current.add(m)
  const restored = result.current.pendingMessagesRef.current
  assert.equal(restored.length, 2, 'both queued messages are restored')
})

test('hydrate([]) PRESERVES an optimistic entry whose POST is still in flight', () => {
  // A reconcile-fetch that lands while an optimistic entry's persistence POST
  // is still in flight must NOT drop it: the server is racing this read.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'inflight-1', ts: 777, content: 're-queued combined' }), { inFlight: true })
  result.current.hydrate([])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1, 'the in-flight optimistic entry survives hydrate([])')
  assert.equal(list[0].cid, 'inflight-1')
  assert.equal(list[0].content, 're-queued combined')
})

test('a server-CONFIRMED add({inFlight:false}) is DROPPED by a later hydrate([])', () => {
  // The fresh-send queued path add()s an already-server-confirmed entry with
  // inFlight:false. In-flight protection is optimistic-only, so a later
  // hydrate([]) the authoritative server list omits is free to drop it.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'confirmed', ts: 123, content: 'server-confirmed', serverTs: true }), { inFlight: false })
  result.current.hydrate([])
  assert.deepEqual(result.current.pendingMessagesRef.current, [],
    'a confirmed entry the server omits is dropped by hydrate, not resurrected')
})

test('explicit preserveMissing keeps a missing visible row but downgrades fast-forward confirmation', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 's-123', ts: 123, content: 'do not drop me', serverTs: true }))
  result.current.hydrate([], { preserveMissing: true })
  let list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1, 'the visible queued row stays visible')
  assert.equal(list[0].content, 'do not drop me')
  assert.equal(list[0].serverTs, false,
    'the row is no longer force-steerable until the server confirms it again')
  assert.equal(list[0].missingFromServer, true)
  result.current.hydrate([{ role: 'user', content: 'do not drop me', ts: 123, cid: 's-123' }])
  list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1)
  assert.equal(list[0].serverTs, true,
    'a later authoritative server row re-confirms fast-forward eligibility')
})

test('an in-flight entry survives hydrate regardless of POST-vs-fetch ordering', () => {
  // Ordering A: fetch (hydrate) wins, THEN the POST confirms.
  const a = renderHook(usePendingQueue)
  a.result.current.add(fixtureMsg({ cid: 'race-a', ts: 500, content: 'combined' }), { inFlight: true })
  a.result.current.hydrate([])
  assert.equal(a.result.current.pendingMessagesRef.current.length, 1,
    'entry survives the reconcile that ran before the POST committed')
  a.result.current.confirmQueued('race-a', { ts: 9001 })
  const afterA = a.result.current.pendingMessagesRef.current
  assert.equal(afterA.length, 1)
  assert.equal(afterA[0].cid, 'race-a', 'cid stays stable through the confirm')
  assert.equal(afterA[0].ts, 9001, 'server ts promoted in')

  // Ordering B: the POST confirms FIRST (no longer in-flight), THEN a reconcile
  // carrying that cid arrives.
  const b = renderHook(usePendingQueue)
  b.result.current.add(fixtureMsg({ cid: 'race-b', ts: 600, content: 'combined' }), { inFlight: true })
  b.result.current.confirmQueued('race-b', { ts: 9002 })
  b.result.current.hydrate([{ role: 'user', content: 'combined', ts: 9002, cid: 'race-b' }])
  const afterB = b.result.current.pendingMessagesRef.current
  assert.equal(afterB.length, 1, 'no duplicate when the server now lists the entry')
  assert.equal(afterB[0].cid, 'race-b', 'reconcile reuses the shared cid')
})

test('confirmQueued clears in-flight so a later hydrate treats the entry as server state', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'committed', ts: 700 }), { inFlight: true })
  result.current.confirmQueued('committed', { ts: 8001 })
  result.current.hydrate([])
  assert.deepEqual(result.current.pendingMessagesRef.current, [],
    'a committed (not in-flight) entry the server omits is dropped by hydrate')
})

test('a cancelled entry does NOT resurrect on a later hydrate', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'cancel-cid', ts: 800 }), { inFlight: true })
  result.current.cancelByCid('cancel-cid')
  result.current.hydrate([])
  assert.deepEqual(result.current.pendingMessagesRef.current, [],
    'cancelByCid entry stays gone across hydrate')
})

test('serverTs gate: optimistic add starts unconfirmed; confirm/hydrate confirm it', () => {
  // Fast-forward (steer) only converts SERVER-confirmed rows; usePendingQueue
  // stamps serverTs true on exactly the confirmed paths.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'opt-1', ts: 111 }), { inFlight: true })
  assert.equal(result.current.pendingMessagesRef.current[0].serverTs, false,
    'an optimistic add is not server-confirmed until its POST acks')
  result.current.confirmQueued('opt-1', { ts: 222 })
  assert.equal(result.current.pendingMessagesRef.current[0].serverTs, true,
    'confirmQueued confirms the entry')
  result.current.add(fixtureMsg({ cid: 'srv-add', ts: 333, serverTs: true }), { inFlight: false })
  assert.equal(
    result.current.pendingMessagesRef.current.find(m => m.cid === 'srv-add').serverTs,
    true, 'a server-confirmed add is confirmed by construction')
  result.current.hydrate([{ role: 'user', content: 'srv', ts: 444, cid: 's444' }])
  assert.equal(result.current.pendingMessagesRef.current[0].serverTs, true,
    'hydrated entries are server-confirmed')
})

test('a promoted entry does NOT resurrect on a later hydrate', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'promote-me', ts: 900 }), { inFlight: true })
  result.current.add(fixtureMsg({ cid: 'keep-me', ts: 901 }), { inFlight: true })
  result.current.promoteManyByCid(['promote-me'])
  result.current.hydrate([])
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['keep-me'],
    'the promoted entry is not resurrected; the still-in-flight sibling survives',
  )
})

test('hydrate matches a still-in-flight optimistic row by its shared cid (no twin)', () => {
  // The POST ack can race the runtime hydrate: the server row arrives (carrying
  // the same cid) before confirmQueued runs. hydrate reconciles by cid — one
  // row, not a duplicate.
  const { result } = renderHook(usePendingQueue)
  result.current.add({ cid: 'local-1', role: 'user', content: 'hello', ts: 100, queued: true }, { inFlight: true })
  result.current.hydrate([{ role: 'user', content: 'hello', ts: 101, cid: 'local-1' }])
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'local-1')
  assert.equal(result.current.pendingMessagesRef.current[0].ts, 101)
  assert.equal(result.current.pendingMessagesRef.current[0].serverTs, true)
})

test('two distinct in-flight rows with identical text stay distinct (cid disambiguates)', () => {
  // No content-identity collapse: two independent sends carry distinct cids and
  // both survive, even with identical text. The server row carries one of the
  // cids and reconciles to exactly that row.
  const { result } = renderHook(usePendingQueue)
  result.current.add({ cid: 'local-1', role: 'user', content: 'same', ts: 100, queued: true }, { inFlight: true })
  result.current.add({ cid: 'local-2', role: 'user', content: 'same', ts: 101, queued: true }, { inFlight: true })
  result.current.hydrate([{ role: 'user', content: 'same', ts: 102, cid: 'local-1' }])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 2, 'the reconciled server row + the still-in-flight sibling')
  assert.ok(list.find(m => m.cid === 'local-1'))
  assert.ok(list.find(m => m.cid === 'local-2'))
})
