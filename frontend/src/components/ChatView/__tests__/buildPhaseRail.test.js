import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  EMPTY_BUILD_PHASE_RAIL,
  accumulateBuildPhase,
  buildPhaseFromEvent,
  buildPhaseRailViewModel,
  latestBuildPhaseAnnouncement,
} from '../buildPhaseRail.js'

test('buildPhaseFromEvent extracts label + ts and trims the label', () => {
  assert.deepEqual(
    buildPhaseFromEvent({ type: 'build_phase', label: '  Storage wired  ', ts: 5 }),
    { label: 'Storage wired', ts: 5 },
  )
})

test('buildPhaseFromEvent rejects non-phase, empty-label, or non-finite ts', () => {
  assert.equal(buildPhaseFromEvent(null), null)
  assert.equal(buildPhaseFromEvent({ type: 'app_built', appId: '7' }), null)
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
