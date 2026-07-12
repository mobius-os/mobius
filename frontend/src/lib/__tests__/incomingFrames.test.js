/**
 * Unit tests for lib/incomingFrames.js — the hidden-incoming-frame registry
 * consulted by Shell's app-error handler during a double-buffered version
 * swap (plain objects stand in for contentWindows; the registry only needs
 * identity).
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/incomingFrames.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  markIncomingFrameWindow,
  clearIncomingFrameWindow,
  isIncomingFrameWindow,
} from '../incomingFrames.js'

test('a marked window is recognized as incoming', () => {
  const win = {}
  markIncomingFrameWindow(win)
  assert.equal(isIncomingFrameWindow(win), true)
})

test('promotion clears membership — the frame speaks for the visible app', () => {
  const win = {}
  markIncomingFrameWindow(win)
  clearIncomingFrameWindow(win)
  assert.equal(isIncomingFrameWindow(win), false)
})

test('an unknown window is not incoming (default Shell path unaffected)', () => {
  assert.equal(isIncomingFrameWindow({}), false)
})

test('null/undefined sources are never incoming and never throw', () => {
  assert.equal(isIncomingFrameWindow(null), false)
  assert.equal(isIncomingFrameWindow(undefined), false)
  markIncomingFrameWindow(null)      // no-op, must not throw
  clearIncomingFrameWindow(null)     // no-op, must not throw
})

test('a discarded incoming stays flagged until GC (its late messages stay ignored)', () => {
  // A discarded frame's contentWindow is null at unmount, so AppCanvas cannot
  // clear it — and must not: queued messages from the rejected frame should
  // keep being ignored. Only promotion clears.
  const win = {}
  markIncomingFrameWindow(win)
  // ... frame is discarded (no clear call happens) ...
  assert.equal(isIncomingFrameWindow(win), true)
})
