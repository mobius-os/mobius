/**
 * Unit tests for resolveStopResend — the shared decision both Stop
 * branches (clean stop + interrupt timeout) use to choose what to
 * re-send. The audit HIGH (fix A) was: the timeout branch ignored
 * clearedPendingTs and re-sent the full snapshot unconditionally,
 * duplicating a message the natural turn-end drain had already
 * consumed. These lock in the cleared-set contract so the two branches
 * can't drift again.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveStopResend } from '../resolveStopResend.js'

function snap(overrides = {}) {
  return { role: 'user', content: 'hi', ts: 100, ...overrides }
}

const combined = (text, attachments = []) => ({ text, attachments })

test('clearedPendingTs === [] does NOT resend (turn-end drain already consumed it)', () => {
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
  const got = resolveStopResend(snapshot, [11, 22], combined('first\nsecond'))
  assert.equal(got.text, 'first\nsecond')
})

test('cleared subset narrows to only the cleared entries', () => {
  const snapshot = [
    snap({ ts: 11, content: 'keep-streaming' }),
    snap({ ts: 22, content: 'only-this-was-cleared' }),
  ]
  // Backend only cleared ts 22 (11 was promoted into the dying turn).
  const got = resolveStopResend(
    snapshot, [22], combined('keep-streaming\nonly-this-was-cleared'),
  )
  assert.equal(got.text, 'only-this-was-cleared',
    'resend only the cleared entry, not the whole combined text')
})

test('unmatched cleared ts (in-flight optimistic) falls back to full combined', () => {
  // The snapshot still holds an OPTIMISTIC ts for a message whose
  // queue-POST was in flight when Stop landed; the backend cleared the
  // SERVER ts, which the snapshot does not carry. A visible resend of
  // the whole combined text beats silently dropping the message.
  const snapshot = [snap({ ts: 11, content: 'optimistic-only' })]
  const got = resolveStopResend(snapshot, [99999], combined('optimistic-only'))
  assert.equal(got.text, 'optimistic-only', 'fall back to full combined')
})

test('null clearedPendingTs (legacy backend) falls back to full combined', () => {
  const snapshot = [snap({ ts: 11, content: 'legacy' })]
  const got = resolveStopResend(snapshot, null, combined('legacy'))
  assert.equal(got.text, 'legacy')
})

test('undefined clearedPendingTs falls back to full combined', () => {
  const snapshot = [snap({ ts: 11, content: 'legacy' })]
  const got = resolveStopResend(snapshot, undefined, combined('legacy'))
  assert.equal(got.text, 'legacy')
})

test('attachments de-dup by name across the resent subset', () => {
  const snapshot = [
    snap({ ts: 11, content: 'a', attachments: [{ name: 'x.png' }, { name: 'y.png' }] }),
    snap({ ts: 22, content: 'b', attachments: [{ name: 'x.png' }, { name: 'z.png' }] }),
  ]
  const got = resolveStopResend(snapshot, [11, 22], combined('a\nb'))
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
