/* Exercise native drawer scroll handoff and the held-pin touch reservation lifecycle. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useWorkspaceDrag from '../useWorkspaceDrag.js'

function eventSurface() {
  const listeners = new Map()
  return {
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(callback)
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback) },
    listenerCount(type) { return listeners.get(type)?.size || 0 },
    fire(type, event) {
      for (const callback of [...(listeners.get(type) || [])]) callback(event)
    },
  }
}

function harness(t, { pinned = true, acceptReorder = true } = {}) {
  const restorers = []
  const window = eventSurface()
  const document = { ...eventSurface(), body: { style: {} } }
  for (const [name, value] of Object.entries({ window, document })) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, value })
    restorers.push(() => {
      if (original) Object.defineProperty(globalThis, name, original)
      else delete globalThis[name]
    })
  }
  let now = 0
  let id = 0
  const timers = new Map()
  const frames = new Map()
  t.mock.method(performance, 'now', () => now)
  t.mock.method(globalThis, 'setTimeout', (callback, delay) => {
    timers.set(++id, { callback, at: now + delay })
    return id
  })
  t.mock.method(globalThis, 'clearTimeout', id => timers.delete(id))
  for (const [name, value] of Object.entries({
    requestAnimationFrame(callback) { frames.set(++id, callback); return id },
    cancelAnimationFrame(id) { frames.delete(id) },
  })) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, value })
    restorers.push(() => {
      if (original) Object.defineProperty(globalThis, name, original)
      else delete globalThis[name]
    })
  }
  const scroller = {
    scrollTop: 200, clientHeight: 700, clientWidth: 340, currentCSSZoom: 1,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 340, height: 700 }),
  }
  const source = {
    ...eventSurface(),
    dataset: { dragKey: 'chat:pin' }, style: {}, blur() {},
    hasAttribute: name => name === 'data-pinned-key' && pinned,
    releasePointerCapture() {},
    closest(selector) {
      if (selector === '[data-drag-key]') return source
      if (selector === '#navigation-drawer') return {}
      if (selector === '.drawer__scroll') return scroller
      return null
    },
  }
  const actions = { reorders: [], menus: [] }
  const hook = renderHook(useWorkspaceDrag, {
    dragActiveRef: { current: false },
    drawerRowGesturesRef: { current: new Map([['chat:pin', { current: {
      beginReorder: args => { actions.reorders.push(args); return acceptReorder },
      openMenu: point => actions.menus.push(point),
    } }]]) },
  })
  t.after(() => {
    hook.unmount()
    actions.reorders.forEach(session => session.releaseHeldTouchPan?.())
    for (const restore of restorers.reverse()) restore()
  })
  function advance(ms) {
    const end = now + ms
    while (true) {
      const next = [...timers].filter(([, timer]) => timer.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0]
      if (!next) break
      timers.delete(next[0]); now = next[1].at; next[1].callback()
    }
    now = end
  }
  function pointer(type, dx = 0, dy = 0) {
    const event = {
      target: source, pointerId: 1, pointerType: 'touch', isPrimary: true,
      button: 0, clientX: 100 + dx, clientY: 400 + dy,
      preventDefault() {},
    }
    ;(type === 'pointerdown' ? document : window).fire(type, event)
  }
  return { scroller, source, actions, pointer, advance, frames, hook }
}

test('a moving pinned swipe keeps native ownership and cannot become a hold', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(20)
  h.pointer('pointermove', 12, -9)
  h.advance(220); h.pointer('pointermove', 12, -30)
  assert.equal(h.scroller.scrollTop, 200, 'the pointer observer never writes a native scroll')
  assert.equal(h.source.listenerCount('touchmove'), 0)
  assert.equal(h.actions.reorders.length, 0)
  assert.equal(h.actions.menus.length, 0)
  h.pointer('pointercancel')
})

test('early small vertical travel cancels the hold instead of reordering a slow swipe', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(100)
  h.pointer('pointermove', 0, -4)
  h.advance(150); h.pointer('pointermove', 0, -8)
  assert.equal(h.scroller.scrollTop, 200)
  assert.equal(h.source.listenerCount('touchmove'), 0)
  assert.equal(h.actions.reorders.length, 0)
  h.pointer('pointercancel')
})

test('a pinned quick flick creates neither a JavaScript pan nor a competing glide', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(20)
  h.pointer('pointermove', 0, -80)
  h.pointer('pointercancel') // Native panning takes over the pointer stream.
  assert.equal(h.scroller.scrollTop, 200)
  assert.equal(h.frames.size, 0)
  assert.equal(h.source.listenerCount('touchmove'), 0)
})

test('a stationary short hold still delegates pinned movement to reorder', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(190)
  h.pointer('pointermove', 0, -10)
  assert.equal(h.actions.reorders.length, 1)
  assert.equal(h.scroller.scrollTop, 200)
  assert.equal(h.source.listenerCount('touchmove'), 1, 'reorder inherits the held reservation')
  let prevented = false
  h.source.fire('touchmove', { touches: [{}], preventDefault() { prevented = true } })
  assert.equal(prevented, true, 'the held first touchmove cannot become native panning')
  h.actions.reorders[0].releaseHeldTouchPan()
  assert.equal(h.source.listenerCount('touchmove'), 0)
})

test('a stationary long hold still opens actions before release', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(610)
  assert.equal(h.actions.menus.length, 1)
  assert.equal(h.source.listenerCount('touchmove'), 1)
  h.pointer('pointerup')
  assert.equal(h.source.listenerCount('touchmove'), 0)
  assert.equal(h.frames.size, 0)
})

test('a left close-swipe yields without reclaiming subsequent vertical movement', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(20)
  h.pointer('pointermove', -20, -2)
  h.pointer('pointermove', -20, -60)
  assert.equal(h.scroller.scrollTop, 200)
  assert.equal(h.frames.size, 0)
})

test('ordinary rows leave scrolling to the browser', t => {
  const h = harness(t, { pinned: false })
  h.pointer('pointerdown'); h.advance(20)
  h.pointer('pointermove', 0, -80)
  assert.equal(h.scroller.scrollTop, 200)
  assert.equal(h.frames.size, 0)
})

test('native takeover clears the pinned observer without a competing momentum owner', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(20)
  h.pointer('pointermove', 0, -80)
  h.pointer('pointercancel')
  assert.equal(h.frames.size, 0)
})

for (const end of ['pointercancel', 'unmount']) {
  test(`a held touch reservation is released on ${end}`, t => {
    const h = harness(t)
    h.pointer('pointerdown'); h.advance(190)
    assert.equal(h.source.listenerCount('touchmove'), 1)
    if (end === 'unmount') h.hook.unmount()
    else h.pointer(end)
    assert.equal(h.source.listenerCount('touchmove'), 0)
  })
}

test('a rejected reorder handoff releases the held reservation', t => {
  const h = harness(t, { acceptReorder: false })
  h.pointer('pointerdown'); h.advance(190); h.pointer('pointermove', 0, -10)
  assert.equal(h.actions.reorders.length, 1)
  assert.equal(h.source.listenerCount('touchmove'), 0)
})

test('the held one-finger reservation does not block a two-finger pinch', t => {
  const h = harness(t)
  h.pointer('pointerdown'); h.advance(190)
  let prevented = false
  h.source.fire('touchmove', { touches: [{}, {}], preventDefault() { prevented = true } })
  assert.equal(prevented, false)
  h.pointer('pointercancel')
  assert.equal(h.source.listenerCount('touchmove'), 0)
})

test('a quick pin press stays nonblocking before the shared hold threshold', t => {
  const h = harness(t)
  h.pointer('pointerdown')
  assert.equal(h.source.listenerCount('touchmove'), 0)
  h.advance(179)
  assert.equal(h.source.listenerCount('touchmove'), 0)
  h.pointer('pointermove', 0, -4)
  h.advance(100)
  assert.equal(h.source.listenerCount('touchmove'), 0, 'intentional motion retires the stationary hold')
  assert.equal(h.actions.reorders.length, 0)
  h.pointer('pointercancel')
})
