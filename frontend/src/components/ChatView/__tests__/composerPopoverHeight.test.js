import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  popoverMaxHeight,
  visibleTopInRectSpace,
  POPOVER_CAP,
} from '../composerPopoverHeight.js'
import { MIN_PANE_H } from '../../Shell/paneModel.js'

test('caps at POPOVER_CAP when there is plenty of room above the trigger', () => {
  // Keyboard down on a 793px-tall phone: trigger near the bottom.
  assert.equal(popoverMaxHeight({ triggerTop: 700 }), POPOVER_CAP)
})

test('fits the space above the trigger when the keyboard shrinks the viewport', () => {
  // The old dvh cap asked for 420px here, which put the panel's top ~120px
  // above the visible area.
  assert.equal(popoverMaxHeight({ triggerTop: 300 }), 284)
})

test('subtracts the visual viewport offset (Android: rects stay layout-relative)', () => {
  // Same trigger position on screen, but the layout viewport has been scrolled
  // down by 200px, so rect.top is 200 larger while the room is unchanged. The
  // trigger's rect (bottom 550) sits BELOW the 400px visible band, which is how
  // the helper knows the rects are still layout-relative.
  assert.equal(popoverMaxHeight({
    triggerTop: 500,
    triggerBottom: 550,
    viewportTop: 200,
    viewportHeight: 400,
  }), 284)
})

test('does not subtract the offset twice when iOS rects are already visual-relative', () => {
  // The reported bug, with numbers read off the iPhone recording: a 874pt
  // layout viewport, keyboard + accessory bar ~421pt, so the visible band is
  // 453pt tall. iOS reports the fixed shell's rects against that visible band —
  // the + button's rect is [363, 413], already keyboard-adjusted — while
  // `offsetTop` still reports the 421pt inset. Subtracting it again gave
  // 363 - 421 - 16 = -74 → clamped to 0 → a 14px empty sliver.
  assert.equal(popoverMaxHeight({
    triggerTop: 363,
    triggerBottom: 413,
    viewportTop: 421,
    viewportHeight: 453,
    clipTop: -301, // `.chat` starts above the visible area in this space
  }), 347)
})

test('stays inside the clipping pane, which is stricter than the viewport', () => {
  // Phone, keyboard up: `.chat` starts at y=99 (below the shell header) and the
  // + button sits at y=294. Anything above 99 is clipped by the pane's
  // `overflow: hidden` and hit-tests to the shell header — the reported bug.
  assert.equal(popoverMaxHeight({ triggerTop: 294, viewportTop: 0, clipTop: 99 }), 179)
})

test('takes the viewport boundary when it is below the pane top', () => {
  assert.equal(popoverMaxHeight({
    triggerTop: 500,
    triggerBottom: 550,
    viewportTop: 200,
    viewportHeight: 400,
    clipTop: 40,
  }), 284)
})

test('never returns more than the space above the trigger', () => {
  // There is no minimum height. A floor here used to return 160 (and 84 with a
  // 100px visible viewport) for these geometries, rendering the panel's top —
  // the Attach files row — above the boundary, where it is clipped away and
  // hit-tests to the shell chrome instead. Short is recoverable; unreachable
  // is not.
  assert.equal(popoverMaxHeight({ triggerTop: 40 }), 24)
  assert.equal(popoverMaxHeight({ triggerTop: 40, viewportHeight: 100 }), 24)
  assert.equal(popoverMaxHeight({ triggerTop: 0 }), 0)
})

test('stays inside a pane at the smallest supported height', () => {
  // MIN_PANE_H is a SUPPORTED surface, not an impossible measurement: split a
  // workspace to the minimum and the composer really does sit ~100px below the
  // pane's clipping top. The panel must fit that, however cramped.
  const clipTop = 500
  const triggerTop = clipTop + (MIN_PANE_H / 2)
  const height = popoverMaxHeight({
    triggerTop,
    triggerBottom: triggerTop + 44,
    clipTop,
    viewportHeight: 900,
  })
  assert.equal(height, 84)
  assert.ok(triggerTop - height >= clipTop, 'panel top must not cross the clipping boundary')
})

test('visibleTopInRectSpace ignores an offset the rects already contain', () => {
  // Trigger fits inside the visible band measured from 0 → visual-relative.
  assert.equal(visibleTopInRectSpace({
    triggerBottom: 413, viewportTop: 421, viewportHeight: 453,
  }), 0)
  // Trigger sits below the band → layout-relative, the offset is real.
  assert.equal(visibleTopInRectSpace({
    triggerBottom: 834, viewportTop: 421, viewportHeight: 453,
  }), 421)
  // No keyboard: nothing to correct for.
  assert.equal(visibleTopInRectSpace({
    triggerBottom: 834, viewportTop: 0, viewportHeight: 874,
  }), 0)
  // No visualViewport support: trust the offset the caller passed.
  assert.equal(visibleTopInRectSpace({
    triggerBottom: 834, viewportTop: 421, viewportHeight: 0,
  }), 421)
})
