/**
 * Cross-unit regression for the Stop-timeout reconcile-clobber race
 * (audit HIGH, fixes A + B together). This is the gap both reviewers
 * flagged: the prior tests exercised usePendingQueue in isolation and
 * never the handleStop → doSend(queue path) → onStreamEnd → hydrate
 * interaction that re-introduced "Stop ate my message" via a new race.
 *
 * A true component-render test of handleStop is infeasible in this
 * node-only harness (no renderer / jsdom). Instead we drive the TWO
 * real units handleStop composes — resolveStopResend (what to resend)
 * and usePendingQueue (the tray + its add/hydrate) — through the exact
 * stop-timeout sequence and assert the combined message SURVIVES in the
 * tray regardless of POST-vs-fetch ordering.
 *
 * Sequence modelled (timeout branch of handleStop):
 *   1. snapshot the queue, then clear() it (Stop's pre-await clear).
 *   2. /chat/stop returns {stopped:false, cleared_pending_ts:[<ts>]} —
 *      the runner timed out; the backend cleared persisted pending.
 *   3. resolveStopResend → text to re-send (NOT empty, since the set is
 *      non-empty and matches the snapshot).
 *   4. doSend's queue path: add() the combined entry (marks it
 *      in-flight) and fire the queueOnly POST (modelled by a later
 *      swapOptimisticTs once it "commits").
 *   5. the still-attached stream finalizes → onStreamEnd(continues:false)
 *      → fetchMessages({force:true}) → hydrate(server pending). Because
 *      the backend already CLEARED server pending, that list is EMPTY.
 *
 * The bug: hydrate([]) wiped the optimistic combined entry if it won the
 * race against the POST commit. The fix: the entry is in-flight, so
 * hydrate preserves it.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import usePendingQueue from '../usePendingQueue.js'
import { resolveStopResend } from '../../resolveStopResend.js'

function queued(overrides = {}) {
  return { role: 'user', content: 'hi', ts: 100, cid: 'cid', queued: true, ...overrides }
}

test('stop-timeout resend survives onStreamEnd hydrate([]) when the FETCH wins the race', () => {
  const { result } = renderHook(usePendingQueue)
  const q = usePendingQueueOps(result)

  // 1. user had a queued message; handleStop snapshots then clears.
  const snapshot = [queued({ cid: 'q1', ts: 11, content: 'finish the task' })]
  for (const m of snapshot) q.add(m)
  q.clear()
  assert.deepEqual(q.list(), [])

  // 2-3. /chat/stop timed out and reports it cleared ts 11. The shared
  // resolver says to resend (non-empty cleared set, full match).
  const combined = { text: 'finish the task', attachments: [] }
  const resend = resolveStopResend(snapshot, [11], combined)
  assert.equal(resend.text, 'finish the task', 'timeout branch resends the cleared message')

  // 4. doSend's queue path: optimistic add (POST now in flight).
  const optimistic = queued({ cid: 'resend-cid', ts: 5000, content: resend.text })
  q.add(optimistic)
  assert.equal(q.list().length, 1)

  // 5. FETCH WINS: onStreamEnd's refetch hydrates the (cleared) empty
  // server pending BEFORE the POST commits.
  q.hydrate([])
  assert.equal(q.list().length, 1, 'combined message survives the reconcile-fetch')
  assert.equal(q.list()[0].content, 'finish the task')

  // POST then commits with the server ts → reconciles in place, cid stable.
  q.swapOptimisticTs('resend-cid', 6001)
  assert.equal(q.list().length, 1)
  assert.equal(q.list()[0].cid, 'resend-cid')
  assert.equal(q.list()[0].ts, 6001)
})

test('stop-timeout resend survives when the POST commits BEFORE the reconcile fetch', () => {
  const { result } = renderHook(usePendingQueue)
  const q = usePendingQueueOps(result)

  const snapshot = [queued({ cid: 'q1', ts: 11, content: 'finish the task' })]
  for (const m of snapshot) q.add(m)
  q.clear()

  const resend = resolveStopResend(snapshot, [11], { text: 'finish the task', attachments: [] })
  const optimistic = queued({ cid: 'resend-cid', ts: 5000, content: resend.text })
  q.add(optimistic)

  // POST WINS: it commits first, so the entry now carries the server ts.
  q.swapOptimisticTs('resend-cid', 6001)
  // onStreamEnd's refetch now reflects the persisted message.
  q.hydrate([{ role: 'user', content: 'finish the task', ts: 6001 }])
  assert.equal(q.list().length, 1, 'no duplicate, no drop')
  assert.equal(q.list()[0].cid, 'resend-cid', 'reconcile reuses the local cid via ts match')
})

test('stop-timeout with an ALREADY-DRAINED queue resends NOTHING (no duplicate)', () => {
  // The natural turn-end drain consumed the queued message right as Stop
  // landed → cleared_pending_ts is []. resolveStopResend returns empty,
  // so handleStop adds nothing; a subsequent hydrate([]) leaves an empty
  // tray. No phantom message, no duplicate follow-up run.
  const { result } = renderHook(usePendingQueue)
  const q = usePendingQueueOps(result)

  const snapshot = [queued({ cid: 'q1', ts: 11, content: 'already drained' })]
  for (const m of snapshot) q.add(m)
  q.clear()

  const resend = resolveStopResend(snapshot, [], { text: 'already drained', attachments: [] })
  assert.equal(resend.text, '', 'nothing to resend')
  // handleStop's `if (resendText)` guard means doSend is NOT called, so
  // nothing is added to the tray.
  if (resend.text) q.add(queued({ cid: 'should-not-happen', ts: 5000, content: resend.text }))
  q.hydrate([])
  assert.deepEqual(q.list(), [], 'tray stays empty — no resurrected/duplicate message')
})

// Thin accessor so the test reads in handleStop's vocabulary.
function usePendingQueueOps(result) {
  return {
    add: (m) => result.current.add(m),
    clear: () => result.current.clear(),
    hydrate: (s) => result.current.hydrate(s),
    swapOptimisticTs: (cid, ts, pos) => result.current.swapOptimisticTs(cid, ts, pos),
    list: () => result.current.pendingMessagesRef.current,
  }
}
