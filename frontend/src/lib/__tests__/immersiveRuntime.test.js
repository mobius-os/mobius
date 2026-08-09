import assert from 'node:assert/strict'
import { test } from 'node:test'

import { makeImmersive } from '../../runtime/immersive.js'

function target() {
  const listeners = new Map()
  return {
    style: {
      cursor: 'help',
      touchAction: 'pan-y',
      userSelect: 'text',
      webkitUserSelect: 'text',
      webkitTouchCallout: 'default',
    },
    listeners,
    addEventListener(type, listener) {
      const values = listeners.get(type) || []
      values.push(listener)
      listeners.set(type, values)
    },
    removeEventListener(type, listener) {
      listeners.set(type, (listeners.get(type) || []).filter(value => value !== listener))
    },
  }
}

test('holdToToggle is idempotent and cleanup restores the target', () => {
  const immersive = makeImmersive({ appId: 7 })
  const element = target()
  const before = { ...element.style }

  const cleanup = immersive.holdToToggle(element)
  const repeated = immersive.holdToToggle(element)

  assert.equal(repeated, cleanup)
  assert.equal(element.listeners.get('pointerdown').length, 1)
  assert.deepEqual(element.style, {
    cursor: 'pointer',
    touchAction: 'none',
    userSelect: 'none',
    webkitUserSelect: 'none',
    webkitTouchCallout: 'none',
  })

  cleanup()
  assert.equal(element.__mobiusHold, undefined)
  assert.deepEqual(element.style, before)
  for (const listeners of element.listeners.values()) assert.equal(listeners.length, 0)
})
