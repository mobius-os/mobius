import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  layoutMayOwnScroll,
  scrollAuthorityAllowsCommit,
  terminalLayoutAuthority,
} from '../scroll/policy.js'

// Contract R5, v1.24 — physical touch contact is itself reader ownership.
// Owner-reported failure (2026-08-22): while a reply streamed, the chat moved
// under a finger that was still on the glass. Root cause: gesture ownership
// was keyed to timers and scroll events only. A reading pause longer than the
// 250ms quiet edge settled the gesture mid-contact, and a resting finger
// outlived the 2s no-scroll dead-man; both handed layout the viewport while
// the finger was still down, so the next streamed chunk wrote scrollTop under
// live touch (reproduced live: a 500px yank under a resting finger).

// ---------------------------------------------------------------------------
// Pure ownership predicates
// ---------------------------------------------------------------------------

test('live touch contact blocks layout ownership even after the timing gate opens', () => {
  // Timing gate open (dead-man released, quiet edge elapsed) — contact still owns.
  assert.equal(layoutMayOwnScroll(0, 5_000, true), false)
  // Same instant without contact releases as before.
  assert.equal(layoutMayOwnScroll(0, 5_000, false), true)
  // Omitted contact keeps the pre-v1.24 call shape working (desktop paths).
  assert.equal(layoutMayOwnScroll(0, 5_000), true)
  // Contact plus a pending input window is doubly blocked.
  assert.equal(layoutMayOwnScroll(Number.POSITIVE_INFINITY, 5_000, true), false)
})

test('a commit with current generation still waits for the finger to lift', () => {
  const base = {
    capturedVersion: 7,
    currentVersion: 7,
    gestureWindowUntil: 0,
    now: 5_000,
  }
  assert.equal(
    scrollAuthorityAllowsCommit({ ...base, touchContactActive: true }),
    false,
  )
  assert.equal(
    scrollAuthorityAllowsCommit({ ...base, touchContactActive: false }),
    true,
  )
})

test('terminal pin settlement treats contact as wait, never as stale', () => {
  const base = {
    capturedVersion: 3,
    currentVersion: 3,
    gestureWindowUntil: 0,
    now: 5_000,
  }
  // Contact defers the armed pin's terminal decision; the plan stays live and
  // retries, exactly like the input-to-first-scroll handoff.
  assert.equal(
    terminalLayoutAuthority({ ...base, touchContactActive: true }),
    'wait',
  )
  assert.equal(
    terminalLayoutAuthority({ ...base, touchContactActive: false }),
    'commit',
  )
  // A genuinely newer gesture is still permanently stale, contact or not.
  assert.equal(
    terminalLayoutAuthority({
      ...base,
      currentVersion: 4,
      touchContactActive: true,
    }),
    'stale',
  )
})
