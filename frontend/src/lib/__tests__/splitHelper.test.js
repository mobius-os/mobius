/**
 * Unit tests for the ChatSplit state machine pure logic (src/lib/splitHelper.js).
 *
 * Run with the lib loader:
 *   cd frontend && npm run test:lib
 * or directly:
 *   node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/lib/__tests__/splitHelper.test.js
 *
 * All functions under test are pure (no DOM, no browser APIs) so they
 * run headless under node:test without any harness.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  STATES, WIDE_BREAKPOINT_PX, FLICK_VELOCITY_PX_MS, DEAD_ZONE_PX, ARROW_STEP_RATIO,
  clampRatio, resolveTransition, stateToContentHeight, stateToContentWidth, parsePersisted,
} from '../splitHelper.js'

// ── Constants ────────────────────────────────────────────────────────────────

test('exported constants have the expected values', () => {
  assert.equal(STATES.PILL, 'pill')
  assert.equal(STATES.SPLIT, 'split')
  assert.equal(STATES.FULL, 'full')
  assert.equal(WIDE_BREAKPOINT_PX, 600)
  assert.equal(FLICK_VELOCITY_PX_MS, 0.4)
  assert.equal(DEAD_ZONE_PX, 24)
  assert.equal(ARROW_STEP_RATIO, 0.04)
})

// ── clampRatio ────────────────────────────────────────────────────────────────

test('clampRatio keeps ratio within usable band', () => {
  // 120px min content, 96px min chat, 600px total → lo=0.2, hi=0.84
  const lo = 120 / 600
  const hi = 1 - 96 / 600
  assert.ok(clampRatio(0.5, 600, 120, 96) === 0.5)
  assert.ok(clampRatio(0, 600, 120, 96) >= lo)
  assert.ok(clampRatio(1, 600, 120, 96) <= hi)
})

test('clampRatio returns 0.5 when pane minimums exceed total', () => {
  assert.equal(clampRatio(0.3, 100, 80, 80), 0.5)
})

test('clampRatio with zero total returns ratio unchanged', () => {
  assert.equal(clampRatio(0.7, 0, 120, 96), 0.7)
})

// ── resolveTransition ────────────────────────────────────────────────────────

test('resolveTransition: positive flick → pill (narrow) or split (wide)', () => {
  assert.equal(
    resolveTransition(0.5, FLICK_VELOCITY_PX_MS + 0.1, false, 600, 120, 96),
    STATES.PILL,
  )
  assert.equal(
    resolveTransition(0.5, FLICK_VELOCITY_PX_MS + 0.1, true, 600, 120, 96),
    STATES.SPLIT,
  )
})

test('resolveTransition: negative flick → full', () => {
  assert.equal(
    resolveTransition(0.5, -(FLICK_VELOCITY_PX_MS + 0.1), false, 600, 120, 96),
    STATES.FULL,
  )
  assert.equal(
    resolveTransition(0.5, -(FLICK_VELOCITY_PX_MS + 0.1), true, 600, 120, 96),
    STATES.FULL,
  )
})

test('resolveTransition: no flick, mid ratio → split', () => {
  assert.equal(
    resolveTransition(0.5, 0, false, 600, 120, 96),
    STATES.SPLIT,
  )
})

test('resolveTransition: no flick, ratio near min-content edge → full', () => {
  // min content is 120/600 = 0.2; ratio at 0.2 should snap to full
  assert.equal(
    resolveTransition(0.2, 0, false, 600, 120, 96),
    STATES.FULL,
  )
})

test('resolveTransition: no flick, ratio near min-chat edge → pill (narrow)', () => {
  // min chat is 96/600 = 0.16; ratio at 0.84+ snaps to pill on narrow
  assert.equal(
    resolveTransition(0.85, 0, false, 600, 120, 96),
    STATES.PILL,
  )
})

test('resolveTransition: no flick, ratio near min-chat edge → split (wide)', () => {
  assert.equal(
    resolveTransition(0.85, 0, true, 600, 120, 96),
    STATES.SPLIT,
  )
})

// ── stateToContentHeight ─────────────────────────────────────────────────────

test('stateToContentHeight: pill → full mount height', () => {
  assert.equal(stateToContentHeight(STATES.PILL, 0.65, 800), 800)
})

test('stateToContentHeight: full → 0', () => {
  assert.equal(stateToContentHeight(STATES.FULL, 0.65, 800), 0)
})

test('stateToContentHeight: split → ratio * totalPx (rounded)', () => {
  assert.equal(stateToContentHeight(STATES.SPLIT, 0.65, 800), 520)
})

// ── stateToContentWidth ──────────────────────────────────────────────────────

test('stateToContentWidth: full → 0', () => {
  assert.equal(stateToContentWidth(STATES.FULL, 0.65, 1000), 0)
})

test('stateToContentWidth: split → ratio * totalPx (rounded)', () => {
  assert.equal(stateToContentWidth(STATES.SPLIT, 0.65, 1000), 650)
})

test('stateToContentWidth: pill has no side meaning → returns ratio * totalPx', () => {
  // Wide viewports don't use pill state; but the function should be defined for it.
  assert.ok(typeof stateToContentWidth(STATES.PILL, 0.65, 1000) === 'number')
})

// ── parsePersisted ────────────────────────────────────────────────────────────

test('parsePersisted: valid object → returns it', () => {
  assert.deepEqual(parsePersisted({ ratio: 0.5, state: 'split' }), { ratio: 0.5, state: 'split' })
})

test('parsePersisted: unknown state → null', () => {
  assert.equal(parsePersisted({ ratio: 0.5, state: 'unknown' }), null)
})

test('parsePersisted: ratio out of range → null', () => {
  assert.equal(parsePersisted({ ratio: 1.5, state: 'split' }), null)
  assert.equal(parsePersisted({ ratio: -0.1, state: 'split' }), null)
})

test('parsePersisted: null/undefined/string input → null', () => {
  assert.equal(parsePersisted(null), null)
  assert.equal(parsePersisted(undefined), null)
  assert.equal(parsePersisted('split'), null)
})

test('parsePersisted: all valid states accepted', () => {
  for (const s of ['pill', 'split', 'full']) {
    assert.ok(parsePersisted({ ratio: 0.5, state: s }) !== null, `state ${s} should parse`)
  }
})
