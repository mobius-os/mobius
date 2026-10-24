/* Idle-slot ranking uses where each block is now, not where it crossed the
   prefetch margin. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderHook } from './react-hook-shim.mjs'
import { useNearViewport, viewportDistance } from '../../markdown/blockViewport.js'
import { createPassiveSlots } from '../../markdown/passiveSlots.js'

test('viewport distance is from the centre of the block to the centre of the viewport', () => {
  assert.equal(viewportDistance({ top: 300, bottom: 400 }, 700), 0)
  assert.equal(viewportDistance({ top: -500, bottom: -400 }, 700), 800)
})

test('with 8 blocks for one app, the block centred on screen gets a frame after scrolling', () => {
  const observers = []
  const listeners = new Map()
  const saved = { IntersectionObserver: globalThis.IntersectionObserver, window: globalThis.window,
    requestAnimationFrame: globalThis.requestAnimationFrame, cancelAnimationFrame: globalThis.cancelAnimationFrame }
  globalThis.IntersectionObserver = class {
    constructor(callback) { this.callback = callback; observers.push(this) }
    observe(node) { this.node = node }
    disconnect() {}
  }
  globalThis.window = { innerHeight: 700,
    addEventListener: (type, fn) => listeners.set(type, [...(listeners.get(type) || []), fn]),
    removeEventListener: (type, fn) => listeners.set(type, (listeners.get(type) || []).filter(other => other !== fn)) }
  globalThis.requestAnimationFrame = fn => { fn(); return 1 }
  globalThis.cancelAnimationFrame = () => {}
  try {
    const slots = createPassiveSlots(3)
    // Block i is 200 px tall at y = 200·i - offset.
    let offset = 0
    const nodes = Array.from({ length: 8 }, (_, i) => ({
      getBoundingClientRect: () => ({ top: 200 * i - offset, bottom: 200 * i - offset + 200 }) }))
    const h = renderHook(() => nodes.map((_, i) => {
      const [ref, view] = useNearViewport()
      // A stand-in for usePassiveSlot: register what PassiveAppBlock would.
      slots.update(1, nodes[i], { wanted: view.near, retained: false, distance: view.distance })
      return { ref, granted: slots.granted(1, nodes[i]) }
    }))
    nodes.forEach((node, i) => h.result.current[i].ref(node))
    // Every block is within the 400 px prefetch margin when observed.
    for (const observer of observers) observer.callback([{ isIntersecting: true }])
    h.rerender()
    // The reader scrolls so block 6 (y 1200-1400) is centred: offset 950.
    offset = 950
    for (const fn of listeners.get('scroll') || []) fn()
    h.rerender()
    const granted = h.result.current.map(block => block.granted)
    assert.equal(granted[6], true, 'the centred block has a frame')
    assert.deepEqual(granted.map((value, i) => value ? i : null).filter(i => i !== null), [5, 6, 7])
  } finally {
    Object.assign(globalThis, saved)
  }
})
