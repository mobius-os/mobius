import test from 'node:test'
import assert from 'node:assert/strict'

import { fitShellToVisualViewport } from '../useShellVisualViewport.js'

function fakeShell(layoutHeight, zoom = 1) {
  const properties = new Map()
  return {
    style: {
      setProperty: (name, value) => properties.set(name, value),
      removeProperty: name => properties.delete(name),
    },
    get clientHeight() {
      const framed = Number.parseFloat(properties.get('height'))
      return Number.isFinite(framed) ? framed : layoutHeight
    },
    offsetWidth: 1000,
    get offsetHeight() { return this.clientHeight },
    currentCSSZoom: zoom,
    getBoundingClientRect() {
      return {
        left: 0,
        top: 0,
        width: 1000 * zoom,
        height: this.clientHeight * zoom,
      }
    },
    property: name => properties.get(name),
  }
}

test('a keyboard overlay fits the shell to the visible viewport', () => {
  const root = fakeShell(860)
  assert.equal(fitShellToVisualViewport(root, {
    height: 492,
    offsetTop: 44,
  }), true)
  assert.equal(root.property('top'), '44px')
  assert.equal(root.property('bottom'), 'auto')
  assert.equal(root.property('height'), '492px')
})

test('desktop author zoom is not mistaken for a software keyboard', () => {
  const root = fakeShell(1000, 0.9)
  assert.equal(fitShellToVisualViewport(root, {
    height: 900,
    offsetTop: 0,
  }), false)
  assert.equal(root.property('height'), undefined)
})

test('the keyboard threshold remains a painted-pixel policy under author zoom', () => {
  const root = fakeShell(1000, 0.9)
  assert.equal(fitShellToVisualViewport(root, {
    height: 821,
    offsetTop: 0,
  }), false)
  assert.equal(root.property('height'), undefined)
})

test('keyboard viewport dimensions cross into zoomed layout space once', () => {
  const root = fakeShell(1000, 0.9)
  assert.equal(fitShellToVisualViewport(root, {
    height: 540,
    offsetTop: 45,
  }), true)
  assert.equal(root.property('top'), '50px')
  assert.equal(root.property('height'), '600px')
})

test('open, close, and open again always remeasure the unframed shell', () => {
  const root = fakeShell(860)
  const keyboardOpen = { height: 492, offsetTop: 0 }

  assert.equal(fitShellToVisualViewport(root, keyboardOpen), true)
  assert.equal(root.property('height'), '492px')

  assert.equal(fitShellToVisualViewport(root, { height: 860, offsetTop: 0 }), false)
  assert.equal(root.property('height'), undefined)

  assert.equal(fitShellToVisualViewport(root, keyboardOpen), true)
  assert.equal(root.property('height'), '492px')
})

test('ordinary layout resizing and small browser chrome keep the CSS frame', () => {
  const resizedRoot = fakeShell(492)
  assert.equal(fitShellToVisualViewport(resizedRoot, { height: 492 }), false)
  assert.equal(resizedRoot.property('height'), undefined)

  const chromeRoot = fakeShell(860)
  assert.equal(fitShellToVisualViewport(chromeRoot, { height: 781 }), false)
  assert.equal(chromeRoot.property('height'), undefined)
})
