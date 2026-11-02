import test from 'node:test'
import assert from 'node:assert/strict'
import { placeContextMenu, placeUnderAnchor } from '../contextMenuGeometry.js'

test('a desktop context menu stays beside its layout-space anchor', () => {
  const position = placeContextMenu({
    point: { x: 380, y: 215 },
    viewport: { width: 1512, height: 861 },
    menuSize: { width: 220, height: 190 },
  })

  assert.deepEqual(position, { x: 388, y: 223 })
})

test('a context menu flips before the right and bottom viewport edges', () => {
  const position = placeContextMenu({
    point: { x: 790, y: 590 },
    viewport: { width: 800, height: 600 },
    menuSize: { width: 220, height: 180 },
  })

  assert.deepEqual(position, { x: 562, y: 402 })
})

test('an oversized context menu clamps to the viewport padding', () => {
  const position = placeContextMenu({
    point: { x: 10, y: 10 },
    viewport: { width: 200, height: 150 },
    menuSize: { width: 240, height: 180 },
  })

  assert.deepEqual(position, { x: 12, y: 12 })
})

test('a popover opens centred under its anchor and moves above it when the bottom has no room', () => {
  const anchor = { left: 300, right: 400, top: 200, bottom: 320 }
  const bounds = { left: 0, top: 0, right: 1200, bottom: 800 }
  const size = { width: 300, height: 160 }
  assert.deepEqual(placeUnderAnchor({ anchor, bounds, size }), { x: 200, y: 328 })
  const low = placeUnderAnchor({ anchor: { ...anchor, top: 640, bottom: 760 }, bounds, size })
  assert.equal(low.y, 472)
})

test('a popover stays inside its pane: at either end of a row it lines up with the pane edge, not the window', () => {
  const bounds = { left: 256, top: 0, right: 1200, bottom: 800 }
  const size = { width: 300, height: 160 }
  const first = placeUnderAnchor({ anchor: { left: 300, right: 400, top: 200, bottom: 320 }, bounds, size })
  assert.equal(first.x, 268)
  const last = placeUnderAnchor({ anchor: { left: 1100, right: 1190, top: 200, bottom: 320 }, bounds, size })
  assert.equal(last.x, 888)
})
