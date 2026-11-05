import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  HOLD_MS,
  HOLD_FRAME_CHARGE_MS,
  chargeAfterFrame,
  SWIPE_DX,
  HOLD_HAPTIC_ENTER_MS,
  HOLD_HAPTIC_EXIT_MS,
  holdComplete,
  releasedAsTap,
  isSwipeRight,
  swipeAllowed,
  movedBeyondSlop,
  decidePointerMove,
  runHoldCompletion,
} from '../logoHoldMachine.js'

// ── Hold threshold ──────────────────────────────────────────────────────────

function chargeOver(frameMs, frames) {
  let charged = 0
  for (let i = 0; i < frames; i += 1) charged = chargeAfterFrame(charged, frameMs)
  return charged
}

test('a hold on a healthy display takes exactly its threshold', () => {
  for (const frameMs of [1000 / 120, 1000 / 60, 1000 / 30, HOLD_FRAME_CHARGE_MS]) {
    const frames = Math.round(HOLD_MS / frameMs)
    assert.equal(holdComplete(chargeOver(frameMs, frames + 1)), true)
    assert.equal(holdComplete(chargeOver(frameMs, frames - 1)), false)
  }
})

test('one long frame cannot turn a tap into a hold', () => {
  // A busy game can hold a tap's release back for most of a second.
  assert.equal(holdComplete(chargeOver(715, 1)), false)
  assert.equal(holdComplete(chargeOver(715, 2)), false)
})

test('a real hold still completes on a starved display, charged frame by frame', () => {
  assert.equal(holdComplete(chargeOver(175, Math.ceil(HOLD_MS / HOLD_FRAME_CHARGE_MS))), true)
})

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

test('movedBeyondSlop trips on small drift, and a TOUCH swipe is classified BEFORE cancel', () => {
  assert.equal(movedBeyondSlop(3, 3), false, 'a jittery tap stays a hold')
  assert.equal(movedBeyondSlop(20, 0), true)
  // A qualifying TOUCH/pen swipe-right is a swipe, never merely a cancel.
  assert.equal(decidePointerMove(SWIPE_DX, 2, 'touch'), 'swipe')
  assert.equal(decidePointerMove(SWIPE_DX, 2, 'pen'), 'swipe')
  // A large NON-swipe drag cancels the hold cleanly.
  assert.equal(decidePointerMove(0, 40, 'touch'), 'cancel')
  assert.equal(decidePointerMove(-30, 0, 'touch'), 'cancel')
  // A tiny in-slop wobble keeps holding.
  assert.equal(decidePointerMove(4, 4, 'touch'), 'continue')
})

test('a MOUSE swipe-right is a cancel, never a mode flip (finding F12)', () => {
  // Only touch/pen may swipe; unknown provenance never swipes.
  assert.equal(swipeAllowed('touch'), true)
  assert.equal(swipeAllowed('pen'), true)
  assert.equal(swipeAllowed('mouse'), false)
  assert.equal(swipeAllowed(undefined), false, 'unknown provenance never swipes')
  // The geometry qualifies, but a mouse drag falls through to cancel — no toggle.
  assert.equal(decidePointerMove(SWIPE_DX + 10, 0, 'mouse'), 'cancel', 'mouse drag → cancel, not swipe')
  assert.equal(decidePointerMove(SWIPE_DX, 2, undefined), 'cancel', 'absent pointerType → cancel')
})

// ── Completion feedback (CHARGE model: haptic + spring/snap), enter AND exit ─

test('runHoldCompletion: entering fires the 12 haptic and starts the spring', () => {
  const calls = []
  const flourishes = []
  runHoldCompletion({
    vibrate: (ms) => calls.push(ms),
    reducedMotion: false,
    entering: true,
    startFlourish: (e) => flourishes.push(e),
  })
  assert.deepEqual(calls, [HOLD_HAPTIC_ENTER_MS], 'the heavier ENTER haptic (12)')
  assert.deepEqual(flourishes, [true], 'the spring starts once, entering=true')
})

test('runHoldCompletion: exiting fires the 8 haptic and starts the snap', () => {
  const calls = []
  const flourishes = []
  runHoldCompletion({
    vibrate: (ms) => calls.push(ms),
    reducedMotion: false,
    entering: false,
    startFlourish: (e) => flourishes.push(e),
  })
  assert.deepEqual(calls, [HOLD_HAPTIC_EXIT_MS], 'the lighter EXIT haptic (8)')
  assert.deepEqual(flourishes, [false], 'the snap starts once, entering=false')
})

test('reduced motion SKIPS the spring/snap but KEEPS the haptic', () => {
  const calls = []
  let flourishes = 0
  runHoldCompletion({
    vibrate: (ms) => calls.push(ms),
    reducedMotion: true,
    entering: true,
    startFlourish: () => { flourishes += 1 },
  })
  assert.deepEqual(calls, [HOLD_HAPTIC_ENTER_MS], 'haptic still fires under reduced motion')
  assert.equal(flourishes, 0, 'the spring/snap motion is skipped')
})

test('runHoldCompletion is a graceful no-op where the Vibration API is absent (iOS)', () => {
  let flourishes = 0
  // No `vibrate` provided — feature-detected absent; must not throw.
  assert.doesNotThrow(() => runHoldCompletion({
    vibrate: undefined, reducedMotion: false, entering: true, startFlourish: () => { flourishes += 1 },
  }))
  assert.equal(flourishes, 1, 'the spring still plays without haptics')
})
