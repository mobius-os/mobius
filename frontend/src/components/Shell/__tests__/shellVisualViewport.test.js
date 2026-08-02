import test from 'node:test'
import assert from 'node:assert/strict'

import { fitShellToVisualViewport } from '../useShellVisualViewport.js'

function fakeShell(layoutHeight) {
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
