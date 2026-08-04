/* Layout-space tests pin the shell's one client-to-layout boundary under author zoom. */

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  captureLayoutSpace,
  clientLengthToLayout,
  clientPointToLayout,
} from '../layoutSpace.js'

function box({
  left = 0,
  top = 0,
  paintedWidth = 900,
  paintedHeight = 720,
  layoutWidth = 1000,
  layoutHeight = 800,
  currentCSSZoom = 0.9,
  clientLeft = 0,
  clientTop = 0,
} = {}) {
  return {
    currentCSSZoom,
    clientLeft,
    clientTop,
    clientWidth: layoutWidth,
    clientHeight: layoutHeight,
    offsetWidth: layoutWidth,
    offsetHeight: layoutHeight,
    ownerDocument: { documentElement: {} },
    getBoundingClientRect: () => ({
      left,
      top,
      width: paintedWidth,
      height: paintedHeight,
    }),
  }
}

test('author-scaled client input crosses into layout space once', () => {
  const space = captureLayoutSpace(box({
    left: 288,
    top: 18,
    clientLeft: 10,
    clientTop: 10,
  }))

  assert.deepEqual(space, {
    clientLeft: 297,
    clientTop: 27,
    width: 1000,
    height: 800,
    zoom: 0.9,
  })
  assert.deepEqual(
    clientPointToLayout({ x: 747, y: 297 }, space),
    { x: 500, y: 300 },
  )
  assert.equal(clientLengthToLayout(90, space), 100)
})

test('the zoomed document root keeps its expanded offset dimensions as layout space', () => {
  const root = box({
    paintedWidth: 1080,
    paintedHeight: 720,
    layoutWidth: 1200,
    layoutHeight: 800,
  })
  root.clientWidth = 1080
  root.clientHeight = 720
  root.ownerDocument.documentElement = root

  const space = captureLayoutSpace(root)

  assert.equal(space.width, 1200)
  assert.equal(space.height, 800)
  assert.deepEqual(clientPointToLayout({ x: 1080, y: 720 }, space), {
    x: 1200,
    y: 800,
  })
})

test('client points and lengths cross the zoom boundary once', () => {
  const space = captureLayoutSpace(box({ left: 288, top: 18 }))

  assert.deepEqual(
    clientPointToLayout({ x: 738, y: 288 }, space),
    { x: 500, y: 300 },
  )
  assert.equal(clientLengthToLayout(-45, space), -50)
})

test('native scale remains an ordinary identity boundary', () => {
  const space = captureLayoutSpace(box({
    left: 20,
    top: 30,
    paintedWidth: 500,
    paintedHeight: 300,
    layoutWidth: 500,
    layoutHeight: 300,
    currentCSSZoom: 1,
  }))

  assert.deepEqual(clientPointToLayout({ x: 55, y: 70 }, space), { x: 35, y: 40 })
  assert.equal(clientLengthToLayout(48, space), 48)
})

test('the document root computed zoom is the legacy fallback', () => {
  const originalGetComputedStyle = globalThis.getComputedStyle
  const root = { authoredZoom: 0.9 }
  const legacy = box({ currentCSSZoom: undefined })
  legacy.ownerDocument.documentElement = root
  globalThis.getComputedStyle = node => ({ zoom: String(node.authoredZoom) })
  let space
  try {
    space = captureLayoutSpace(legacy)
  } finally {
    globalThis.getComputedStyle = originalGetComputedStyle
  }
  assert.equal(space.zoom, 0.9)
})

test('currentCSSZoom does not mistake transform scaling for CSS zoom', () => {
  const transformed = box({
    currentCSSZoom: 0.9,
    paintedWidth: 720,
    paintedHeight: 576,
  })
  const space = captureLayoutSpace(transformed)
  assert.equal(space.zoom, 0.9)
})

test('currentCSSZoom keeps zero-sized measurement fallbacks finite', () => {
  const zero = {
    currentCSSZoom: 0.9,
    offsetWidth: 0,
    offsetHeight: 0,
    clientWidth: 0,
    clientHeight: 0,
    getBoundingClientRect: () => ({ left: 4, top: 6, width: 0, height: 0 }),
  }
  const space = captureLayoutSpace(zero)

  assert.equal(space.zoom, 0.9)
  assert.deepEqual(clientPointToLayout({ x: 13, y: 15 }, space), { x: 10, y: 10 })
})
