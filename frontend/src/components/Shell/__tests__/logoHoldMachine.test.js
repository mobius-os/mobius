import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  HOLD_MS,
  SWIPE_DX,
  HOLD_HAPTIC_MS,
  holdComplete,
  releasedAsTap,
  isSwipeRight,
  movedBeyondSlop,
  decidePointerMove,
  runHoldCompletion,
} from '../logoHoldMachine.js'

// ── Hold threshold ──────────────────────────────────────────────────────────

test('holdComplete only past the threshold; a shorter press is a tap', () => {
  assert.equal(holdComplete(HOLD_MS - 1), false)
  assert.equal(holdComplete(HOLD_MS), true)
  assert.equal(holdComplete(HOLD_MS + 50), true)
  // releasedAsTap is the exact inverse — an early release opens the drawer.
  assert.equal(releasedAsTap(HOLD_MS - 1), true)
  assert.equal(releasedAsTap(HOLD_MS), false)
})

// ── Swipe vs cancel ─────────────────────────────────────────────────────────

test('isSwipeRight needs rightward travel past the threshold AND horizontal dominance', () => {
  assert.equal(isSwipeRight(SWIPE_DX, 0), true)
  assert.equal(isSwipeRight(SWIPE_DX - 1, 0), false, 'below threshold')
  assert.equal(isSwipeRight(SWIPE_DX + 10, SWIPE_DX + 20), false, 'more vertical than horizontal')
  assert.equal(isSwipeRight(-40, 0), false, 'leftward is not a swipe-right')
})

test('movedBeyondSlop trips on small drift, and swipe is classified BEFORE cancel', () => {
  assert.equal(movedBeyondSlop(3, 3), false, 'a jittery tap stays a hold')
  assert.equal(movedBeyondSlop(20, 0), true)
  // A qualifying swipe-right is a swipe, never merely a cancel.
  assert.equal(decidePointerMove(SWIPE_DX, 2), 'swipe')
  // A large NON-swipe drag cancels the hold cleanly.
  assert.equal(decidePointerMove(0, 40), 'cancel')
  assert.equal(decidePointerMove(-30, 0), 'cancel')
  // A tiny in-slop wobble keeps holding.
  assert.equal(decidePointerMove(4, 4), 'continue')
})

// ── Completion feedback (haptic + pulse), enter AND exit ────────────────────

test('runHoldCompletion fires the haptic once and starts the pulse', () => {
  const calls = []
  let pulses = 0
  runHoldCompletion({
    vibrate: (ms) => calls.push(ms),
    reducedMotion: false,
    startPulse: () => { pulses += 1 },
  })
  assert.deepEqual(calls, [HOLD_HAPTIC_MS], 'exactly one short haptic pulse')
  assert.equal(pulses, 1, 'the outward pulse starts once')
})

test('reduced motion SKIPS the pulse but KEEPS the haptic', () => {
  const calls = []
  let pulses = 0
  runHoldCompletion({
    vibrate: (ms) => calls.push(ms),
    reducedMotion: true,
    startPulse: () => { pulses += 1 },
  })
  assert.deepEqual(calls, [HOLD_HAPTIC_MS], 'haptic still fires under reduced motion')
  assert.equal(pulses, 0, 'the motion pulse is skipped')
})

test('runHoldCompletion is a graceful no-op where the Vibration API is absent (iOS)', () => {
  let pulses = 0
  // No `vibrate` provided — feature-detected absent; must not throw.
  assert.doesNotThrow(() => runHoldCompletion({
    vibrate: undefined, reducedMotion: false, startPulse: () => { pulses += 1 },
  }))
  assert.equal(pulses, 1, 'the visual pulse still plays without haptics')
})
