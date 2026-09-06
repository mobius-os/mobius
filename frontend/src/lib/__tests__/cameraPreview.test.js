import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  clampCameraPreviewRect,
  readCameraPreviewRect,
} from '../cameraPreview.js'

test('camera preview geometry accepts only finite positive rectangles', () => {
  assert.deepEqual(
    readCameraPreviewRect({ x: -20, y: 10, width: 320, height: 180 }),
    { x: -20, y: 10, width: 320, height: 180 },
  )
  assert.equal(readCameraPreviewRect({ x: 0, y: 0, width: 0, height: 180 }), null)
  assert.equal(readCameraPreviewRect({ x: 0, y: 0, width: '320', height: 180 }), null)
  assert.equal(readCameraPreviewRect({ x: NaN, y: 0, width: 320, height: 180 }), null)
})

test('camera preview geometry is clipped to the owning app canvas', () => {
  assert.deepEqual(clampCameraPreviewRect(
    { x: -20, y: 40, width: 360, height: 220 },
    { width: 300, height: 200 },
  ), {
    x: 0, y: 40, width: 300, height: 160,
  })
  assert.equal(clampCameraPreviewRect(
    { x: 310, y: 40, width: 60, height: 60 },
    { width: 300, height: 200 },
  ), null)
})
