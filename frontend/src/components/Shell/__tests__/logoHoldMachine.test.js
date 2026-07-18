import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  HOLD_MS,
  SWIPE_DX,
  HOLD_HAPTIC_ENTER_MS,
  HOLD_HAPTIC_EXIT_MS,
  holdComplete,
  releasedAsTap,
  isSwipeRight,
  movedBeyondSlop,
  decidePointerMove,
  runHoldCompletion,
  haloFrame,
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

// ── Living halo drift (pure, allocation-free) ───────────────────────────────

test('haloFrame drifts within bounds, reuses its out object, and never repeats', () => {
  const out = {}
  const r = haloFrame(1234, out)
  assert.equal(r, out, 'writes into the SAME object (no per-frame allocation)')
  // Sample a spread of times; every field stays in a sane, subtle range.
  for (const t of [0, 500, 1500, 9000, 123456]) {
    const f = haloFrame(t, out)
    assert.ok(f.scale > 0.9 && f.scale < 1.1, `scale in range @${t}`)
    assert.ok(Math.abs(f.x) <= 4 && Math.abs(f.y) <= 4, `offset small @${t}`)
    assert.ok(f.opacity > 0.6 && f.opacity <= 1, `opacity in range @${t}`)
  }
  // Irrational-ratio sines: two far-apart times are not identical (no loop).
  const a = haloFrame(1000, {})
  const b = haloFrame(1000 + 60000, {})
  assert.notDeepEqual(a, b, 'the glow does not visibly loop')
})
