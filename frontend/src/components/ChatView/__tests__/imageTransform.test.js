import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  clampImageScale,
  clampImageTransform,
  zoomImageAround,
} from '../markdown/imageTransform.js'

const metrics = {
  baseWidth: 800,
  baseHeight: 600,
  viewportWidth: 1000,
  viewportHeight: 800,
}

test('image scale stays within the natural viewer range', () => {
  assert.equal(clampImageScale(0.2), 1)
  assert.equal(clampImageScale(2.5), 2.5)
  assert.equal(clampImageScale(9), 5)
})

test('returning to fitted size recentres the image', () => {
  assert.deepEqual(
    clampImageTransform({ scale: 1, x: 300, y: -200 }, metrics),
    { scale: 1, x: 0, y: 0 },
  )
})

test('panning is clamped so an enlarged image cannot be lost off-screen', () => {
  assert.deepEqual(
    clampImageTransform({ scale: 2, x: 999, y: -999 }, metrics),
    { scale: 2, x: 300, y: -200 },
  )
})

test('pointer-centred zoom keeps the inspected point under the pointer', () => {
  const result = zoomImageAround(
    { scale: 1, x: 0, y: 0 },
    2,
    { x: 650, y: 350 },
    { x: 500, y: 400 },
    metrics,
  )
  assert.deepEqual(result, { scale: 2, x: -150, y: 50 })
})
