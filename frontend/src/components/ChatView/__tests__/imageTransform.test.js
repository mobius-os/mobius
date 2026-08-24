import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  clampImageScale,
  clampImageTransform,
  hasPanRoom,
  imageScaleCeiling,
  readingZoomScale,
  wheelScrollPans,
  zoomImageAround,
} from '../markdown/imageTransform.js'

const metrics = {
  baseWidth: 800,
  baseHeight: 600,
  viewportWidth: 1000,
  viewportHeight: 800,
}

// A 750×8000 screenshot fitted to a 1000×800 viewport is an 80px-wide strip;
// reading it needs scales well past the default 5× ceiling.
const tallStrip = {
  baseWidth: 80,
  baseHeight: 800,
  viewportWidth: 1000,
  viewportHeight: 800,
  naturalWidth: 750,
  dpr: 1,
}

test('image scale stays within the natural viewer range', () => {
  assert.equal(clampImageScale(0.2), 1)
  assert.equal(clampImageScale(2.5), 2.5)
  assert.equal(clampImageScale(9), 5)
})

test('the ceiling parameter is a true ceiling in both directions', () => {
  assert.equal(clampImageScale(9, 9.4), 9)
  assert.equal(clampImageScale(20, 9.4), 9.4)
  assert.equal(clampImageScale(4, 3), 3)
})

test('the zoom ceiling reaches 1:1 device pixels for downscaled images', () => {
  assert.equal(imageScaleCeiling(tallStrip), 750 / 80)
  // On a 2× display half the CSS scale already reaches device pixels.
  assert.equal(imageScaleCeiling({ ...tallStrip, dpr: 2 }), 5)
  // Without natural measurements the default ceiling holds (legacy metrics).
  assert.equal(imageScaleCeiling(metrics), 5)
})

test('metrics flow through transform clamping and zooming', () => {
  const clamped = clampImageTransform({ scale: 9, x: 0, y: -99999 }, tallStrip)
  assert.equal(clamped.scale, 9)
  assert.equal(clamped.y, -(800 * 9 - 800) / 2)
  const zoomed = zoomImageAround(
    { scale: 1, x: 0, y: 0 }, 12, { x: 500, y: 400 }, { x: 500, y: 400 }, tallStrip,
  )
  assert.equal(zoomed.scale, 750 / 80)
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

test('pan engages on room, not zoom level, so a toolbar-hidden strip stays reachable', () => {
  // Fully visible at fit: no room, no pan.
  assert.equal(hasPanRoom(1, tallStrip), false)
  // A small image zoomed but still inside the screen has nothing to pan.
  assert.equal(hasPanRoom(1.2, { ...metrics, baseWidth: 400, baseHeight: 300 }), false)
  // Fitted to the layout viewport but taller than the visible (visual)
  // viewport — a mobile toolbar case: pan must engage at 1× and the clamp
  // must allow reaching the hidden ends instead of pinning to centre.
  const toolbarHidden = { ...tallStrip, baseHeight: 800, viewportHeight: 640 }
  assert.equal(hasPanRoom(1, toolbarHidden), true)
  assert.equal(wheelScrollPans({ scale: 1, x: 0, y: 0 }, 0, 100, toolbarHidden), true)
  assert.deepEqual(
    clampImageTransform({ scale: 1, x: 0, y: -999 }, toolbarHidden),
    { scale: 1, x: 0, y: -80 },
  )
})

test('plain scroll pans only an image with room on the dominant axis', () => {
  // Fitted image: nothing to pan.
  assert.equal(wheelScrollPans({ scale: 1, x: 0, y: 0 }, 0, 100, tallStrip), false)
  // Zoomed tall strip has vertical room.
  assert.equal(wheelScrollPans({ scale: 9, x: 0, y: 0 }, 0, 100, tallStrip), true)
  // A wide image zoomed slightly has no vertical room — vertical wheel must
  // stay a zoom so the wheel is never dead...
  const wide = { ...metrics, baseWidth: 968, baseHeight: 242 }
  assert.equal(wheelScrollPans({ scale: 2, x: 0, y: 0 }, 0, 100, wide), false)
  // ...while a dominant horizontal scroll with horizontal room pans.
  assert.equal(wheelScrollPans({ scale: 2, x: 0, y: 0 }, 100, 10, wide), true)
})

test('reading zoom targets legible width without upscaling past native pixels', () => {
  // Tall strip: native sharpness (9.375×) runs out before reading width.
  assert.equal(readingZoomScale(tallStrip), 750 / 80)
  // On a 2× display native sharpness runs out sooner still.
  assert.equal(readingZoomScale({ ...tallStrip, dpr: 2 }), 750 / (80 * 2))
  // A narrower viewport makes reading width the binding limit.
  assert.equal(readingZoomScale({ ...tallStrip, viewportWidth: 500 }), (500 - 32) / 80)
  // A small image keeps the classic 2× instead of blowing up into blur.
  const thumb = {
    baseWidth: 400, baseHeight: 300,
    viewportWidth: 1400, viewportHeight: 800,
    naturalWidth: 400, dpr: 2,
  }
  assert.equal(readingZoomScale(thumb), 2)
})
