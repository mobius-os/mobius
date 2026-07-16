import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  EMPTY_BUILD_PHASE_RAIL,
  accumulateBuildPhase,
  buildPhaseFromEvent,
  buildPhaseRailViewModel,
  latestBuildPhaseAnnouncement,
  railAtRunStart,
} from '../buildPhaseRail.js'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

test('buildPhaseFromEvent extracts label + ts and trims the label', () => {
  assert.deepEqual(
    buildPhaseFromEvent({ type: 'build_phase', label: '  Storage wired  ', ts: 5 }),
    { label: 'Storage wired', ts: 5 },
  )
})

test('buildPhaseFromEvent rejects non-phase, empty-label, or non-finite ts', () => {
  assert.equal(buildPhaseFromEvent(null), null)
  assert.equal(buildPhaseFromEvent({ type: 'app_updated', appId: '7' }), null)
  assert.equal(buildPhaseFromEvent({ type: 'build_phase', label: '   ', ts: 1 }), null)
  assert.equal(buildPhaseFromEvent({ type: 'build_phase', ts: 1 }), null)
  assert.equal(buildPhaseFromEvent({ type: 'build_phase', label: 'x' }), null)
  assert.equal(buildPhaseFromEvent({ type: 'build_phase', label: 'x', ts: 'nope' }), null)
})

test('accumulateBuildPhase appends phases in emission order', () => {
  let rail = EMPTY_BUILD_PHASE_RAIL
  rail = accumulateBuildPhase(rail, { type: 'build_phase', label: 'A', ts: 1 })
  rail = accumulateBuildPhase(rail, { type: 'build_phase', label: 'B', ts: 2 })
  assert.deepEqual(rail, [{ label: 'A', ts: 1 }, { label: 'B', ts: 2 }])
})

test('accumulateBuildPhase dedupes a replayed phase by ts (catch-up safe)', () => {
  const rail = [{ label: 'A', ts: 1 }]
  // Replaying the same ts is a no-op and returns the SAME reference so the
  // caller can skip a re-render — this is what makes catch-up replay safe.
  const after = accumulateBuildPhase(rail, { type: 'build_phase', label: 'A', ts: 1 })
  assert.equal(after, rail)
})

test('accumulateBuildPhase returns the same reference for an invalid event', () => {
  const rail = [{ label: 'A', ts: 1 }]
  assert.equal(accumulateBuildPhase(rail, { type: 'app_updated' }), rail)
  assert.equal(accumulateBuildPhase(rail, null), rail)
})

test('buildPhaseRailViewModel marks only the most recent phase current', () => {
  assert.deepEqual(
    buildPhaseRailViewModel([{ label: 'A', ts: 1 }, { label: 'B', ts: 2 }]),
    [
      { ts: 1, label: 'A', current: false },
      { ts: 2, label: 'B', current: true },
    ],
  )
  assert.deepEqual(buildPhaseRailViewModel([]), [])
})

test('latestBuildPhaseAnnouncement announces the newest phase, empty when none', () => {
  assert.equal(latestBuildPhaseAnnouncement([]), '')
  assert.equal(latestBuildPhaseAnnouncement(null), '')
  assert.equal(
    latestBuildPhaseAnnouncement([{ label: 'A', ts: 1 }, { label: 'Storage wired', ts: 2 }]),
    'Build phase: Storage wired',
  )
})

test('railAtRunStart resets to the shared empty rail', () => {
  assert.equal(railAtRunStart(), EMPTY_BUILD_PHASE_RAIL)
})

test('build rail is the final footer status strip directly above the composer', () => {
  const footStart = chatView.indexOf('<div ref={footRef} className="chat__foot">')
  const composer = chatView.indexOf('<ChatInputBar', footStart)
  const foot = chatView.slice(footStart, composer)
  const rail = foot.indexOf('className="chat__build-rail"')

  assert.ok(footStart >= 0 && composer > footStart && rail >= 0,
    'the footer, build rail, and composer must all be present')
  for (const notice of [
    'className="chat__open-app"',
    'className="chat__question-nudge"',
    'className="chat__resume-nudge"',
    '<ConnectionStatus',
    '<QueuedMessages',
  ]) {
    const noticeIndex = foot.indexOf(notice)
    assert.ok(noticeIndex >= 0, `${notice} must be present in the footer`)
    assert.ok(noticeIndex < rail,
      `${notice} must stack above the build rail, never between it and the composer`)
  }
})

test('a send that merely enqueues preserves the in-flight build rail', () => {
  // An enqueued message emits NO rail transition — the rail changes only
  // through accumulate (build_phase) and railAtRunStart (a run actually
  // starting). The mid-build sequence is: phases accumulate, the owner
  // queues a follow-up (nothing happens to the rail), phases keep landing.
  let rail = accumulateBuildPhase(EMPTY_BUILD_PHASE_RAIL, {
    type: 'build_phase', label: 'First layer openable', ts: 1,
  })
  const railAtEnqueue = rail
  // The enqueue itself runs no transition; incidental stream events around
  // it leave the rail untouched by reference.
  assert.equal(accumulateBuildPhase(rail, { type: 'app_updated', appId: '7' }), railAtEnqueue)
  rail = accumulateBuildPhase(rail, {
    type: 'build_phase', label: 'Storage wired', ts: 2,
  })
  assert.deepEqual(rail.map(p => p.label), ['First layer openable', 'Storage wired'])
})

test('replay after a run-start reset reconstructs only current-run phases', () => {
  // Reconnect ordering across a queue-drain run boundary: the OLD run's log
  // replays [A1, A2, queued_turn_starting(reset)], then the NEW run's log
  // replays [B1]. Applying the transitions in replay order must land on the
  // new run's phases alone — an old phase can never survive the boundary.
  let rail = EMPTY_BUILD_PHASE_RAIL
  rail = accumulateBuildPhase(rail, { type: 'build_phase', label: 'A1', ts: 1 })
  rail = accumulateBuildPhase(rail, { type: 'build_phase', label: 'A2', ts: 2 })
  rail = railAtRunStart()
  rail = accumulateBuildPhase(rail, { type: 'build_phase', label: 'B1', ts: 3 })
  assert.deepEqual(rail, [{ label: 'B1', ts: 3 }])

  // A second reconnect mid-new-run replays B1 again: deduped by ts, and the
  // reference is stable so React skips the re-render.
  const replayed = accumulateBuildPhase(rail, {
    type: 'build_phase', label: 'B1', ts: 3,
  })
  assert.equal(replayed, rail)
})
