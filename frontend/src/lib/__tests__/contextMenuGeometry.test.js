import test from 'node:test'
import assert from 'node:assert/strict'
import { placeContextMenu } from '../contextMenuGeometry.js'

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
