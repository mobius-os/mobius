import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  layoutMayOwnScroll,
  scrollAuthorityAllowsCommit,
  terminalLayoutAuthority,
} from '../scroll/policy.js'

// Contract R5, v1.23 — physical touch contact is itself reader ownership.
// Owner-reported failure (2026-08-22): while a reply streamed, the chat moved
// under a finger that was still on the glass. Root cause: gesture ownership
// was keyed to timers and scroll events only. A reading pause longer than the
// 250ms quiet edge settled the gesture mid-contact, and a resting finger
// outlived the 2s no-scroll dead-man; both handed layout the viewport while
// the finger was still down, so the next streamed chunk wrote scrollTop under
// live touch (reproduced live: a 500px yank under a resting finger).

const ownerSource = readFileSync(
  new URL('../useScrollMode.js', import.meta.url), 'utf8',
)

// ---------------------------------------------------------------------------
// Pure ownership predicates
// ---------------------------------------------------------------------------

test('live touch contact blocks layout ownership even after the timing gate opens', () => {
  // Timing gate open (dead-man released, quiet edge elapsed) — contact still owns.
  assert.equal(layoutMayOwnScroll(0, 5_000, true), false)
  // Same instant without contact releases as before.
  assert.equal(layoutMayOwnScroll(0, 5_000, false), true)
  // Omitted contact keeps the pre-v1.23 call shape working (desktop paths).
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

// ---------------------------------------------------------------------------
// Controller source-shape guards (same idiom as scrollOwnership.test.js)
// ---------------------------------------------------------------------------

test('the quiet-edge settlement defers while a finger is on the glass', () => {
  assert.match(
    ownerSource,
    /const settleReaderScroll = \(\) => \{[\s\S]*?if \(touchContactActive\(\)\) return[\s\S]*?if \(!gesture\.dirty\) return/,
    'settleReaderScroll must check live touch contact before committing a '
    + 'settled mode; a mid-contact settle hands layout the viewport under '
    + 'the reader\'s finger',
  )
})

test('the no-scroll dead-man re-arms instead of releasing through live contact', () => {
  assert.match(
    ownerSource,
    /const releasePendingGesture = \(sequence\) => \{[\s\S]*?if \(touchContactActive\(\)\) \{[\s\S]*?PENDING_GESTURE_CAP_MS\)[\s\S]*?return[\s\S]*?\}[\s\S]*?'reader:no-scroll-release'/,
    'a resting finger is not an interrupted gesture: the dead-man must re-arm '
    + 'its bounded cap under contact rather than releasing layout ownership',
  )
})

test('contact is tracked through touch events, with window and visibility wedge guards', () => {
  // Pointer events stop at the pointercancel a native pan fires; touch events
  // keep firing through the pan, so they are the contact authority.
  for (const listener of [
    /scrollEl\.addEventListener\('touchstart', onTouchContactChange/,
    /scrollEl\.addEventListener\('touchend', onTouchContactChange/,
    /scrollEl\.addEventListener\('touchcancel', onTouchContactChange/,
    /window\.addEventListener\('touchend', onWindowTouchContactEnd/,
    /window\.addEventListener\('touchcancel', onWindowTouchContactEnd/,
    /document\.addEventListener\('visibilitychange', onVisibilityHiddenClearContact/,
  ]) {
    assert.match(ownerSource, listener,
      'contact tracking must survive element replacement and backgrounding '
      + 'mid-gesture, or a stale contact count freezes layout forever')
  }
  assert.match(
    ownerSource,
    /const onTouchContactChange = \(event\) => \{[\s\S]*?event\.touches[\s\S]*?\.length/,
    'the browser\'s own touches.length is the finger count authority, so a '
    + 'multi-touch episode ends exactly when the last finger lifts',
  )
})

test('the last lift restarts the quiet edge so one contact episode settles once', () => {
  assert.match(
    ownerSource,
    /const onTouchContactChange = \(event\) => \{[\s\S]*?if \(gesture\.dirty\) \{[\s\S]*?setTimeout\(settleReaderScroll, GESTURE_SETTLE_MS\)/,
    'a dirty gesture must settle from the lift, not from a mid-contact timer',
  )
})

test('every commit-authority check inside the controller carries touch contact', () => {
  const calls = ownerSource.split('scrollAuthorityAllowsCommit({').slice(1)
  assert.ok(calls.length >= 4,
    'the controller\'s authority funnel went missing; this guard is vacuous')
  for (const call of calls) {
    const argsBlock = call.slice(0, call.indexOf('})'))
    assert.match(argsBlock, /touchContactActive/,
      'an authority check without the contact input reopens the mid-contact '
      + 'write lapse for its call site')
  }
  assert.match(
    ownerSource,
    /terminalLayoutAuthority\(\{[\s\S]*?touchContactActive/,
    'the armed-pin terminal decision must also wait for the finger to lift',
  )
})

test('the gesture record lives in a ref so a mid-stream effect re-run cannot settle it', () => {
  assert.match(
    ownerSource,
    /const readerGestureRef = useRef\(\{[\s\S]*?dirty: false/,
    'gesture intent must survive controller reinstalls (a transcript row '
    + 'committing mid-stream re-runs the layout effect mid-gesture)',
  )
  assert.match(
    ownerSource,
    /const touchContactCountRef = useRef\(0\)/,
    'contact must survive controller reinstalls for the same reason',
  )
})
