/*
 * Where a steer's CUT is published, and who reconciles the tray.
 *
 * `steered_into_turn` is the client's only "seal the live stream here and
 * re-base it" signal. Publishing it at HTTP arrival while the transcript split
 * waited for the Claude runner's interrupt boundary is what made a live turn
 * paint duplicated output for the rest of the turn: every block streamed in
 * between was folded into the sealed pre-steer message AND left at the head of
 * the client's re-based stream. The cut now comes from the seal itself.
 *
 * The event POSITION and the resulting durable order are proven by execution in
 * backend/tests/test_chats_stream_steer.py (the cut lands after the last
 * pre-steer block and before the first continuation block, on the sink's own
 * broadcast). What these tests pin is the client half of the same contract:
 * ONE steer event, ONE tray reconciler per steer, and an order-independent
 * reconcile on the deferred path. The queue mechanics that make it
 * order-independent are executed against the real hook in
 * hooks/__tests__/usePendingQueue.test.js.
 */

import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const streamSource = readFileSync(
  new URL('../useStreamConnection.js', import.meta.url),
  'utf8',
)
const chatViewSource = readFileSync(
  new URL('../ChatView.jsx', import.meta.url),
  'utf8',
)

function sliceBranch(source, fromNeedle, toNeedle) {
  const from = source.indexOf(fromNeedle)
  assert.ok(from >= 0, `expected to find ${fromNeedle}`)
  const to = source.indexOf(toNeedle, from)
  assert.ok(to > from, `expected to find ${toNeedle} after ${fromNeedle}`)
  return source.slice(from, to)
}

test('a steer has exactly one event and one tray reconciler', () => {
  // The send's own 202 already carries `cut_deferred` + the still-queued row,
  // so a second "accepted" SSE event would be a parallel channel reconciling
  // the same tray from the same pre-cut snapshot — racing the response it
  // duplicates. The steer wire has one event: the cut.
  assert.ok(
    !streamSource.includes('steer_accepted'),
    'no second steer event may reconcile the tray beside the 202',
  )
  assert.ok(
    !chatViewSource.includes('onSteerAccepted'),
    'ChatView takes the accepted row from its own response, not a second event',
  )
  const steerEvents = [...streamSource.matchAll(/event\.type === '(steer[^']*)'/g)]
    .map(m => m[1])
  assert.deepEqual(steerEvents, ['steered_into_turn'])
})

test('only the cut re-bases the stream, and a replay refetches instead', () => {
  const cut = sliceBranch(
    streamSource,
    "event.type === 'steered_into_turn'",
    "event.type === 'done'",
  )
  assert.match(
    cut, /flushBuffer\(\)/,
    'the buffered pre-steer frame belongs to the sealed segment, not the next',
  )
  const replay = sliceBranch(cut, 'if (isCatchUp) {', '} else {')
  assert.match(
    replay, /catchUpItems = \[\]/,
    'the cut is the boundary a reconnect reconstructs: drop everything '
    + 'replayed before it, keep the continuation',
  )
  assert.ok(
    !replay.includes('onSteeredIntoTurnRef'),
    'promoting replayed items would duplicate the already-sealed segment',
  )
  assert.match(
    replay, /onNeedsRefreshRef\.current\?\./,
    'dropping the replayed segment is only truthful if the sealed message and '
    + 'the steered row are loaded — a socket that died before the cut arrived '
    + 'live has neither, so the replay asks for the authoritative read',
  )
})

test('the cut hands the steered rows off the tray and into the transcript', () => {
  const handler = sliceBranch(
    chatViewSource,
    'onSteeredIntoTurn: ({',
    '\n  })\n',
  )
  assert.match(
    handler,
    /promoteStreamToMessages\(\{ keepTurnOpen: true \}\)/,
    'the cut seals the live segment as its own message',
  )
  assert.match(
    handler,
    /pendingQueue\.cancelByCid\(cid\)/,
    'the cut is when the rows genuinely leave chat.pending_messages',
  )
})

test('a deferred steer resolves only its OWN row, in any order', () => {
  // The 202's `pending_messages` is a snapshot from steer time, so the runner's
  // cut can already have retired the row by the time it resolves. Confirming
  // one cid is a no-op then; reconciling the whole tray against the snapshot
  // would resurrect that row and drop anything queued since (proven on the
  // real hook in usePendingQueue.test.js).
  const steeredBranch = sliceBranch(
    chatViewSource,
    "if (result?.status === 'steered') {",
    "if (result?.status === 'started') {",
  )
  const deferred = sliceBranch(steeredBranch, 'if (result.cut_deferred) {', '} else {')
  assert.match(
    deferred,
    /pendingQueue\.confirmQueued\(queuedMsg\.cid, \{/,
    'the deferred 202 confirms this send\'s row and keeps it in the tray',
  )
  assert.ok(
    !deferred.includes('pendingQueue.hydrate'),
    'a wholesale reconcile against the pre-cut snapshot is what drops a '
    + 'concurrently-queued row and resurrects a retired one',
  )
  assert.match(
    deferred,
    /forgetQueuedPinIntent\(\{ cid: queuedMsg\.cid \}\)/,
    'the cut reads the pin intent from inlineSteerPinIntentRef, so the map '
    + 'entry must be released here or it leaks for the life of the chat',
  )
  assert.match(
    steeredBranch,
    /\} else \{[\s\S]*?pendingQueue\.cancelByCid\(queuedMsg\.cid\)/,
    'a route-split (Codex) steer still drops the tray entry immediately',
  )
})

test('every steered branch resolves the optimistic in-flight mark', () => {
  // `clearInFlight`'s fallthrough deliberately excludes `steered`, so a steered
  // branch that resolves nothing would leak the mark forever and every later
  // hydrate would preserve the row as a permanent ghost chip.
  const steeredBranch = sliceBranch(
    chatViewSource,
    "if (result?.status === 'steered') {",
    "if (result?.status === 'started') {",
  )
  for (const resolver of [
    /pendingQueue\.confirmQueued\(/,
    /pendingQueue\.cancelByCid\(/,
  ]) {
    assert.match(steeredBranch, resolver)
  }
  // Neither resolver may sit behind a shape check on the response body: a
  // stripped/older `pending_messages` must not leave the mark set.
  const deferred = sliceBranch(steeredBranch, 'if (result.cut_deferred) {', '} else {')
  const confirmAt = deferred.indexOf('pendingQueue.confirmQueued(')
  const guardAt = deferred.indexOf('Array.isArray(result.pending_messages)')
  assert.ok(guardAt >= 0 && confirmAt > guardAt)
  assert.ok(
    !/if \(Array\.isArray\(result\.pending_messages\)\) \{[\s\S]*?confirmQueued/
      .test(deferred),
    'the confirm must run for every deferred response shape, not only when the '
    + 'server echoed a usable queue',
  )
})

test('the fast-forward path keeps an accepted deferred row hidden until the cut', () => {
  const ff = sliceBranch(
    chatViewSource,
    'async function steerRowsImpl(steerRowsList) {',
    '\n  // STEER (fast-forward): inject the queued messages into the LIVE turn',
  )
  const deferred = sliceBranch(ff, 'if (result.cut_deferred) {', '} else if (')
  assert.match(
    ff,
    /pendingQueue\.reserveForSteer\(consumePendingCids\)/,
    'the accepted rows leave the actionable tray before the request',
  )
  assert.ok(
    !/releaseSteerReservation|pendingQueue\.hydrate|pendingQueue\.cancelByCid/
      .test(deferred),
    'a deferred acknowledgement must not re-show or retire rows before the cut',
  )
  assert.match(
    ff,
    /if \(result\?\.status !== 'steered'\)[\s\S]*?releaseSteerReservation\(consumePendingCids\)/,
    'only a rejected request should return the rows to the actionable tray',
  )
})
