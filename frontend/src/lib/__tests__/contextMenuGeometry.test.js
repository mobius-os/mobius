import test from 'node:test'
import assert from 'node:assert/strict'
import { placeContextMenu } from '../contextMenuGeometry.js'

test('a desktop context menu stays beside the painted pointer through root zoom', () => {
  const position = placeContextMenu({
    clientPoint: { x: 380, y: 215 },
    clientViewport: { left: 0, top: 0, width: 1512, height: 861 },
    layoutViewport: { width: 1680, height: 956.6667 },
    menuSize: { width: 220, height: 190 },
  })

  assert.ok(Math.abs(position.x * 0.9 - 388) < 0.1)
  assert.ok(Math.abs(position.y * 0.9 - 223) < 0.1)
})

test('a context menu flips before the right and bottom viewport edges', () => {
  const position = placeContextMenu({
    clientPoint: { x: 790, y: 590 },
    clientViewport: { left: 0, top: 0, width: 800, height: 600 },
    layoutViewport: { width: 800, height: 600 },
    menuSize: { width: 220, height: 180 },
  })

  assert.deepEqual(position, { x: 562, y: 402 })
})

test('an oversized context menu clamps to the viewport padding', () => {
  const position = placeContextMenu({
    clientPoint: { x: 10, y: 10 },
    clientViewport: { left: 0, top: 0, width: 200, height: 150 },
    layoutViewport: { width: 200, height: 150 },
    menuSize: { width: 240, height: 180 },
  })

  assert.deepEqual(position, { x: 12, y: 12 })
})
