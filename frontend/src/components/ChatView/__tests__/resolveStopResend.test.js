/**
 * Unit tests for resolveStopResend — the shared decision both Stop
 * branches (clean stop + interrupt timeout) use to choose what to
 * re-send. The audit HIGH (fix A) was: the timeout branch ignored the
 * cleared set and re-sent the full snapshot unconditionally, duplicating
 * a message the natural turn-end drain had already consumed. These lock
 * in the cleared-set contract so the two branches can't drift again.
 *
 * The cleared set is now keyed on the stable `cid` (cleared_pending_cids)
 * — the snapshot entries carry a cid, and matching is by cidOf.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  collapseQueued,
  ownerQueuedSnapshot,
  resolveStopResend,
  rowsLostToUnansweredStop,
} from '../resolveStopResend.js'

function snap(overrides = {}) {
  const ts = overrides.ts ?? 100
  return { role: 'user', content: 'hi', ts, cid: `c-${ts}`, ...overrides }
}

// -- ownerQueuedSnapshot: machine carriers are never owner speech ----------
test('ownerQueuedSnapshot drops hidden machine carriers, keeps owner text', () => {
  const snapshot = [
    snap({ ts: 11, content: 'owner typed this' }),
    {
      role: 'user', ts: 12, cid: 'wait-result-abc', hidden: true,
      kind: 'wait_result', content: '<wait_result>...</wait_result>',
    },
    snap({ ts: 13, content: 'and this' }),
  ]
  const got = ownerQueuedSnapshot(snapshot)
  assert.deepEqual(got.map(m => m.cid), ['c-11', 'c-13'])
})

test('ownerQueuedSnapshot on a hidden-only queue returns nothing to resend', () => {
  const snapshot = [
    {
      role: 'user', ts: 1, cid: 'wait-result-xyz', hidden: true,
      kind: 'wait_result', content: '<wait_result>...</wait_result>',
    },
  ]
  const owner = ownerQueuedSnapshot(snapshot)
  assert.deepEqual(owner, [])
  // With no owner-typed rows, the collapsed resend text is empty — the
  // phantom wait result is never re-sent as an owner turn.
  const got = resolveStopResend(owner, [])
  assert.equal(got.text, '')
})

test('ownerQueuedSnapshot tolerates null/undefined and malformed rows', () => {
  assert.deepEqual(ownerQueuedSnapshot(null), [])
  assert.deepEqual(ownerQueuedSnapshot(undefined), [])
  assert.deepEqual(ownerQueuedSnapshot([null, undefined]), [])
})

test('cleared_pending_cids === [] does NOT resend (turn-end drain already consumed it)', () => {
  // This is the exact double-send the timeout branch used to commit.
  const snapshot = [snap({ ts: 11, content: 'queued' })]
  const got = resolveStopResend(snapshot, [])
  assert.equal(got.text, '', 'empty cleared set means resend nothing')
  assert.deepEqual(got.attachments, [])
})

test('non-empty cleared set with full match resends exactly that subset', () => {
  const snapshot = [
    snap({ ts: 11, content: 'first' }),
    snap({ ts: 22, content: 'second' }),
  ]
  const got = resolveStopResend(snapshot, ['c-11', 'c-22'])
  assert.equal(got.text, 'first\nsecond')
})

test('cleared subset narrows to only the cleared entries', () => {
  const snapshot = [
    snap({ ts: 11, content: 'keep-streaming' }),
    snap({ ts: 22, content: 'only-this-was-cleared' }),
  ]
  // Backend only cleared cid c-22 (c-11 was promoted into the dying turn).
  const got = resolveStopResend(snapshot, ['c-22'])
  assert.equal(got.text, 'only-this-was-cleared',
    'resend only the cleared entry, not the whole combined text')
})

test('unmatched cleared cid (in-flight optimistic) falls back to full combined', () => {
  // The snapshot holds a row whose cid the backend's cleared set does not
  // name (a mismatch). A visible resend of the whole combined text beats
  // silently dropping the message.
  const snapshot = [snap({ ts: 11, content: 'optimistic-only' })]
  const got = resolveStopResend(snapshot, ['c-99999'])
  assert.equal(got.text, 'optimistic-only', 'fall back to full combined')
})

test('legacy cid-less snapshot cancels via legacy-<ts> derivation', () => {
  // A pre-cid row (no cid) derives legacy-<ts>; the backend's cleared set
  // carries the same derived value.
  const snapshot = [{ role: 'user', content: 'legacy row', ts: 55 }]
  const got = resolveStopResend(snapshot, ['legacy-55'])
  assert.equal(got.text, 'legacy row')
})

test('null cleared set (legacy backend) falls back to full combined', () => {
  const snapshot = [snap({ ts: 11, content: 'legacy' })]
  const got = resolveStopResend(snapshot, null)
  assert.equal(got.text, 'legacy')
})

test('undefined cleared set falls back to full combined', () => {
  const snapshot = [snap({ ts: 11, content: 'legacy' })]
  const got = resolveStopResend(snapshot, undefined)
  assert.equal(got.text, 'legacy')
})

test('attachments de-dup by name across the resent subset', () => {
  const snapshot = [
    snap({ ts: 11, content: 'a', attachments: [{ name: 'x.png' }, { name: 'y.png' }] }),
    snap({ ts: 22, content: 'b', attachments: [{ name: 'x.png' }, { name: 'z.png' }] }),
  ]
  const got = resolveStopResend(snapshot, ['c-11', 'c-22'])
  assert.deepEqual(
    got.attachments.map(a => a.name).sort(),
    ['x.png', 'y.png', 'z.png'],
    'duplicate x.png collapsed',
  )
})

test('empty cleared set returns no attachments even if the snapshot had some', () => {
  const snapshot = [snap({ ts: 11, content: 'q', attachments: [{ name: 'f.pdf' }] })]
  const got = resolveStopResend(snapshot, [])
  assert.equal(got.text, '')
  assert.deepEqual(got.attachments, [], 'nothing cleared → no attachments to resend')
})

// -- collapseQueued: one message from the queued rows -------------------------
test('collapseQueued joins text by line and de-duplicates files by name', () => {
  const got = collapseQueued([
    snap({ ts: 1, content: ' first ', attachments: [{ name: 'a.png' }] }),
    snap({ ts: 2, content: '', attachments: [{ name: 'a.png' }, { name: 'b.pdf' }] }),
    snap({ ts: 3, content: 'second' }),
  ])
  assert.equal(got.text, 'first\nsecond')
  assert.deepEqual(got.attachments.map(a => a.name), ['a.png', 'b.pdf'])
})

// -- rowsLostToUnansweredStop: what an unanswered request may have cleared --
const lostEvidence = (overrides = {}) => ({
  clearedPendingCids: [], serverPending: [], deliveredCids: new Set(), ...overrides,
})

test('rows an unanswered stop request cleared return to the composer', () => {
  // The first request cleared c-11 and c-22 but its reply was lost; the
  // accepted retry cleared nothing more.
  const snapshot = [snap({ ts: 11, content: 'first' }), snap({ ts: 22, content: 'second' })]
  const lost = rowsLostToUnansweredStop(snapshot, lostEvidence())
  assert.deepEqual(lost.map(row => row.cid), ['c-11', 'c-22'])
})

test('rows resent, still held by the server, or already in the transcript are not lost', () => {
  const snapshot = [
    snap({ ts: 11, content: 'resent' }),
    snap({ ts: 22, content: 'held' }),
    snap({ ts: 33, content: 'drained into a turn' }),
    snap({ ts: 44, content: 'lost' }),
  ]
  const lost = rowsLostToUnansweredStop(snapshot, lostEvidence({
    clearedPendingCids: ['c-11'],
    serverPending: [{ cid: 'c-22', ts: 22 }],
    deliveredCids: new Set(['c-33']),
  }))
  assert.deepEqual(lost.map(row => row.cid), ['c-44'])
})

test('nothing is lost when the resend already falls back to every row', () => {
  const snapshot = [snap({ ts: 11, content: 'a' }), snap({ ts: 22, content: 'b' })]
  assert.deepEqual(rowsLostToUnansweredStop(snapshot, lostEvidence({ clearedPendingCids: null })), [])
  assert.deepEqual(
    rowsLostToUnansweredStop(snapshot, lostEvidence({ clearedPendingCids: ['c-99'] })), [],
    'an unmatched cleared cid resends the whole snapshot',
  )
})

test('an unread server queue cannot vouch for a row', () => {
  const snapshot = [snap({ ts: 11, content: 'a' })]
  const lost = rowsLostToUnansweredStop(snapshot, lostEvidence({ serverPending: null }))
  assert.deepEqual(lost.map(row => row.cid), ['c-11'])
})
