/* Drive the brand gesture with real pointer sequences: touch taps resolve once, holds and cancellations never tap. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import { useLogoModeGesture } from '../useLogoModeGesture.js'

function harness(t, props = {}) {
  const winListeners = new Map()
  const listen = (map) => ({
    addEventListener(type, cb) { if (!map.has(type)) map.set(type, new Set()); map.get(type).add(cb) },
    removeEventListener(type, cb) { map.get(type)?.delete(cb) },
  })
  const docListeners = new Map()
  const elListeners = new Map()
  const define = (name, value) => {
    const original = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
    t.after(() => { if (original) Object.defineProperty(globalThis, name, original); else delete globalThis[name] })
  }
  define('window', { ...listen(winListeners), matchMedia: () => ({ matches: false }) })
  define('document', { ...listen(docListeners), visibilityState: 'visible' })
  define('requestAnimationFrame', () => 1)
  define('cancelAnimationFrame', () => {})
  const el = {
    ...listen(elListeners),
    style: { setProperty() {} },
    setPointerCapture() {},
    releasePointerCapture() {},
  }
  const taps = []
  const modes = []
  const args = { drawerOpen: false, ...props }
  const hook = renderHook((p) => useLogoModeGesture({
    brandRef: { current: el },
    onTap: () => taps.push(1),
    onToggleMode: (cause) => { modes.push(cause); return { changed: true, animated: false } },
    ...p,
  }), args)
  const ev = (fields) => ({ button: 0, isPrimary: true, clientX: 10, clientY: 10, pointerType: 'touch', pointerId: 1, timeStamp: 0, preventDefault() {}, ...fields })
  return {
    g: () => hook.result.current,
    taps, modes, elListeners, winListeners,
    rerender: (p) => hook.rerender({ ...args, ...p }),
    down: (f) => hook.result.current.onPointerDown(ev(f)),
    move: (f) => hook.result.current.onPointerMove(ev(f)),
    up: (f) => hook.result.current.onPointerUp(ev(f)),
    cancel: (f) => hook.result.current.onPointerCancel(ev(f)),
    unmount: hook.unmount,
  }
}

test('the brand cancels a single-finger touchstart so the browser runs no long press', (t) => {
  const h = harness(t)
  const [onTouchStart] = h.elListeners.get('touchstart')
  let prevented = 0
  onTouchStart({ touches: [1], cancelable: true, preventDefault: () => { prevented += 1 } })
  onTouchStart({ touches: [1, 2], cancelable: true, preventDefault: () => { prevented += 1 } })
  assert.equal(prevented, 1, 'only the single-finger touchstart is cancelled')
  h.unmount()
  assert.equal(h.elListeners.get('touchstart').size, 0)
})

test('a touch tap, including a drifting thumb, activates exactly once and guards a stray click', (t) => {
  const h = harness(t)
  h.down({ timeStamp: 0 }); h.up({ timeStamp: 120 })
  assert.equal(h.taps.length, 1)
  assert.equal(h.g().consumeSuppressedClick(1), true, 'a compat click after the tap is swallowed')
  h.down({ timeStamp: 1000 }); h.move({ clientX: 24, timeStamp: 1050 }); h.up({ clientX: 24, timeStamp: 1100 })
  assert.equal(h.taps.length, 2, 'a 14px drift past the hold slop is still a tap')
  h.down({ timeStamp: 2000 }); h.move({ clientY: 50, timeStamp: 2050 }); h.up({ clientY: 50, timeStamp: 2100 })
  assert.equal(h.taps.length, 2, 'a 40px drag is not a tap')
})

test('a mouse keeps its native click and a pen barrel button is ignored', (t) => {
  const h = harness(t)
  h.down({ pointerType: 'mouse', timeStamp: 0 }); h.up({ pointerType: 'mouse', timeStamp: 100 })
  assert.equal(h.taps.length, 0)
  h.down({ pointerType: 'pen', button: 2, timeStamp: 200 }); h.up({ pointerType: 'pen', button: 2, timeStamp: 300 })
  assert.equal(h.taps.length, 0)
})

test('a secondary touch, with another finger already down elsewhere, never taps or holds', (t) => {
  const h = harness(t)
  h.down({ pointerId: 7, isPrimary: false, timeStamp: 0 }); h.up({ pointerId: 7, isPrimary: false, timeStamp: 100 })
  h.down({ pointerId: 8, isPrimary: false, timeStamp: 200 }); h.up({ pointerId: 8, isPrimary: false, timeStamp: 900 })
  assert.equal(h.taps.length, 0)
  assert.deepEqual(h.modes, [])
})

test('a second finger can neither steal nor clear the first finger\'s tap', (t) => {
  const h = harness(t)
  h.down({ pointerId: 1, timeStamp: 0 })
  h.up({ pointerId: 2, timeStamp: 40 })
  h.cancel({ pointerId: 2, timeStamp: 50 })
  assert.equal(h.taps.length, 0)
  h.up({ pointerId: 1, timeStamp: 100 })
  assert.equal(h.taps.length, 1)
})

test('a completed hold, a swipe, or an outside cancellation never also taps', (t) => {
  const h = harness(t)
  h.down({ timeStamp: 0 }); h.up({ timeStamp: 600 })
  assert.deepEqual(h.modes, ['hold']); assert.equal(h.taps.length, 0)
  h.down({ timeStamp: 1000 }); h.move({ clientX: 60, timeStamp: 1050 }); h.up({ clientX: 60, timeStamp: 1100 })
  assert.deepEqual(h.modes, ['hold', 'swipe']); assert.equal(h.taps.length, 0)
  // Drawer opened elsewhere mid-press.
  h.down({ timeStamp: 2000 }); h.rerender({ drawerOpen: true }); h.up({ timeStamp: 2100 })
  assert.equal(h.taps.length, 0)
  h.rerender({ drawerOpen: false })
  // Window blur after a drift abandoned the hold but kept the tap candidate.
  h.down({ timeStamp: 3000 }); h.move({ clientX: 24, timeStamp: 3050 })
  for (const cb of h.winListeners.get('blur')) cb()
  h.up({ clientX: 24, timeStamp: 3100 })
  assert.equal(h.taps.length, 0)
})
