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
  splitTrayAfterUnacceptedStop,
} from '../resolveStopResend.js'

function snap(overrides = {}) {
  const ts = overrides.ts ?? 100
  return { role: 'user', content: 'hi', ts, cid: `c-${ts}`, ...overrides }
}

const combined = (text, attachments = []) => ({ text, attachments })

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
  const got = resolveStopResend(owner, [], combined(''))
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
  const got = resolveStopResend(snapshot, [], combined('queued'))
  assert.equal(got.text, '', 'empty cleared set means resend nothing')
  assert.deepEqual(got.attachments, [])
})

test('non-empty cleared set with full match resends exactly that subset', () => {
  const snapshot = [
    snap({ ts: 11, content: 'first' }),
    snap({ ts: 22, content: 'second' }),
  ]
  const got = resolveStopResend(snapshot, ['c-11', 'c-22'], combined('first\nsecond'))
  assert.equal(got.text, 'first\nsecond')
})

test('cleared subset narrows to only the cleared entries', () => {
  const snapshot = [
    snap({ ts: 11, content: 'keep-streaming' }),
    snap({ ts: 22, content: 'only-this-was-cleared' }),
  ]
  // Backend only cleared cid c-22 (c-11 was promoted into the dying turn).
  const got = resolveStopResend(
    snapshot, ['c-22'], combined('keep-streaming\nonly-this-was-cleared'),
  )
  assert.equal(got.text, 'only-this-was-cleared',
    'resend only the cleared entry, not the whole combined text')
})

test('unmatched cleared cid (in-flight optimistic) falls back to full combined', () => {
  // The snapshot holds a row whose cid the backend's cleared set does not
  // name (a mismatch). A visible resend of the whole combined text beats
  // silently dropping the message.
  const snapshot = [snap({ ts: 11, content: 'optimistic-only' })]
  const got = resolveStopResend(snapshot, ['c-99999'], combined('optimistic-only'))
  assert.equal(got.text, 'optimistic-only', 'fall back to full combined')
})

test('legacy cid-less snapshot cancels via legacy-<ts> derivation', () => {
  // A pre-cid row (no cid) derives legacy-<ts>; the backend's cleared set
  // carries the same derived value.
  const snapshot = [{ role: 'user', content: 'legacy row', ts: 55 }]
  const got = resolveStopResend(snapshot, ['legacy-55'], combined('legacy row'))
  assert.equal(got.text, 'legacy row')
})

test('null cleared set (legacy backend) falls back to full combined', () => {
  const snapshot = [snap({ ts: 11, content: 'legacy' })]
  const got = resolveStopResend(snapshot, null, combined('legacy'))
  assert.equal(got.text, 'legacy')
})

test('undefined cleared set falls back to full combined', () => {
  const snapshot = [snap({ ts: 11, content: 'legacy' })]
  const got = resolveStopResend(snapshot, undefined, combined('legacy'))
  assert.equal(got.text, 'legacy')
})

test('attachments de-dup by name across the resent subset', () => {
  const snapshot = [
    snap({ ts: 11, content: 'a', attachments: [{ name: 'x.png' }, { name: 'y.png' }] }),
    snap({ ts: 22, content: 'b', attachments: [{ name: 'x.png' }, { name: 'z.png' }] }),
  ]
  const got = resolveStopResend(snapshot, ['c-11', 'c-22'], combined('a\nb'))
  assert.deepEqual(
    got.attachments.map(a => a.name).sort(),
    ['x.png', 'y.png', 'z.png'],
    'duplicate x.png collapsed',
  )
})

test('empty cleared set returns no attachments even if the snapshot had some', () => {
  const snapshot = [snap({ ts: 11, content: 'q', attachments: [{ name: 'f.pdf' }] })]
  const got = resolveStopResend(snapshot, [], combined('q', [{ name: 'f.pdf' }]))
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

// -- splitTrayAfterUnacceptedStop: the server's queue decides -----------------
const confirmed = (ts, extra = {}) => snap({ ts, content: `q${ts}`, serverTs: true, ...extra })

test('rows the server still holds go back to the tray as its copy', () => {
  const tray = [confirmed(1), confirmed(2)]
  const server = [{ cid: 'c-1', ts: 501, content: 'q1 (server)' }, { cid: 'c-2', ts: 502, content: 'q2' }]
  const { restore, returned } = splitTrayAfterUnacceptedStop(tray, server)
  assert.deepEqual(restore.map(row => [row.cid, row.ts, row.serverTs]), [
    ['c-1', 501, true], ['c-2', 502, true],
  ])
  assert.equal(restore[0].content, 'q1 (server)')
  assert.deepEqual(returned, { text: '', attachments: [] })
})

test('confirmed owner rows the server cleared return to the composer, files included', () => {
  // The stop reply was lost, or a 5xx came after the server cleared the
  // queue: restoring these to the tray would let the next refresh drop them.
  const tray = [confirmed(1, { attachments: [{ name: 'f.png' }] }), confirmed(2)]
  const { restore, returned } = splitTrayAfterUnacceptedStop(tray, [{ cid: 'c-2', ts: 2 }])
  assert.deepEqual(restore.map(row => row.cid), ['c-2'])
  assert.equal(returned.text, 'q1')
  assert.deepEqual(returned.attachments, [{ name: 'f.png' }])
})

test('rows the server never confirmed stay in the tray with the send that owns them', () => {
  // An in-flight queue POST or the durable outbox still owns these;
  // returning them to the composer as well would duplicate them.
  const tray = [
    snap({ ts: 1, content: 'posting', serverTs: false }),
    snap({ ts: 2, content: 'outbox', localOnly: true, serverTs: false }),
  ]
  const { restore, returned } = splitTrayAfterUnacceptedStop(tray, [])
  assert.deepEqual(restore, tray)
  assert.equal(returned.text, '')
})

test('a cleared row already in the transcript is not returned again', () => {
  const tray = [confirmed(1), confirmed(2)]
  const { restore, returned } = splitTrayAfterUnacceptedStop(tray, [], new Set(['c-1']))
  assert.deepEqual(restore, [])
  assert.equal(returned.text, 'q2')
})

test('a hidden carrier the server no longer holds is dropped, never owner text', () => {
  const carrier = confirmed(1, { hidden: true, content: '<wait_result/>' })
  const { restore, returned } = splitTrayAfterUnacceptedStop([carrier], [])
  assert.deepEqual(restore, [])
  assert.equal(returned.text, '')
  const held = splitTrayAfterUnacceptedStop([carrier], [{ cid: 'c-1', hidden: true }])
  assert.deepEqual(held.restore.map(row => row.cid), ['c-1'])
})

test('an unreadable server queue returns confirmed owner rows rather than risk losing them', () => {
  const tray = [confirmed(1), snap({ ts: 2, content: 'posting', serverTs: false })]
  const { restore, returned } = splitTrayAfterUnacceptedStop(tray, null)
  assert.deepEqual(restore.map(row => row.cid), ['c-2'])
  assert.equal(returned.text, 'q1')
})
