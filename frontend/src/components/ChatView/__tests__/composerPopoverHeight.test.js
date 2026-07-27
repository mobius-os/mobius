import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  popoverMaxHeight,
  POPOVER_CAP,
} from '../composerPopoverHeight.js'

test('caps at POPOVER_CAP when there is plenty of room above the trigger', () => {
  // Keyboard down on a 793px-tall phone: trigger near the bottom.
  assert.equal(popoverMaxHeight({ triggerTop: 700 }), POPOVER_CAP)
})

test('fits the space above the trigger when the keyboard shrinks the viewport', () => {
  // The old dvh cap asked for 420px here, which put the panel's top ~120px
  // above the visible area.
  assert.equal(popoverMaxHeight({ triggerTop: 300 }), 284)
})

test('subtracts the visual viewport offset (iOS scrolls the layout viewport)', () => {
  // Same trigger position on screen, but iOS has scrolled the layout viewport
  // down by 200px, so rect.top is 200 larger while the room is unchanged.
  assert.equal(popoverMaxHeight({ triggerTop: 500, viewportTop: 200 }), 284)
})

test('stays inside the clipping pane, which is stricter than the viewport', () => {
  // Phone, keyboard up: `.chat` starts at y=99 (below the shell header) and the
  // + button sits at y=294. Anything above 99 is clipped by the pane's
  // `overflow: hidden` and hit-tests to the shell header — the reported bug.
  assert.equal(popoverMaxHeight({ triggerTop: 294, viewportTop: 0, clipTop: 99 }), 179)
})

test('takes the viewport boundary when it is below the pane top', () => {
  assert.equal(popoverMaxHeight({ triggerTop: 500, viewportTop: 200, clipTop: 40 }), 284)
})

test('never overflows the available space in a cramped pane', () => {
  assert.equal(popoverMaxHeight({ triggerTop: 40 }), 24)
  assert.equal(popoverMaxHeight({ triggerTop: 0 }), 0)
})
