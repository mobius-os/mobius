/* Exercise the right-click, keyboard, and touch-hold gestures that open an item's actions menu. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useItemMenuGestures from '../../Drawer/useItemMenuGestures.js'
import { PRESS_MENU_HOLD_MS, PRE_HOLD_MOVE_PX } from '../dragController.js'

function harness(t) {
  const listeners = new Map()
  const window = {
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(callback)
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback) },
  }
  const original = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: window })
  t.after(() => {
    if (original) Object.defineProperty(globalThis, 'window', original)
    else delete globalThis.window
  })

  let now = 1000
  let id = 0
  const timers = new Map()
  t.mock.method(performance, 'now', () => now)
  t.mock.method(globalThis, 'setTimeout', (callback, delay) => {
    timers.set(++id, { callback, at: now + delay })
    return id
  })
  t.mock.method(globalThis, 'clearTimeout', timer => timers.delete(timer))

  const opened = []
  const hook = renderHook(() => useItemMenuGestures(
    (point, trigger, options) => opened.push({ point, trigger, options }),
  ))
  return {
    gestures: () => hook.result.current,
    opened,
    unmount: hook.unmount,
    advance(ms) {
      now += ms
      for (const [timer, entry] of [...timers]) {
        if (entry.at > now) continue
        timers.delete(timer)
        entry.callback()
      }
    },
    fireWindow(type, event) {
      for (const callback of [...(listeners.get(type) || [])]) callback(event)
    },
  }
}

function syntheticEvent(fields) {
  return {
    defaultPrevented: false,
    propagationStopped: false,
    preventDefault() { this.defaultPrevented = true },
    stopPropagation() { this.propagationStopped = true },
    ...fields,
  }
}

const row = { id: 'project-chat-row' }

test('Shift+F10 and the ContextMenu key open the menu with keyboard focus', t => {
  const h = harness(t)
  for (const key of [{ key: 'F10', shiftKey: true }, { key: 'ContextMenu' }]) {
    const event = syntheticEvent({ type: 'keydown', ...key, clientX: 0, clientY: 0, currentTarget: row })
    h.gestures().triggerProps.onKeyDown(event)
    assert.equal(event.defaultPrevented, true)
  }
  h.gestures().triggerProps.onKeyDown(syntheticEvent({ type: 'keydown', key: 'F10', currentTarget: row }))
  h.gestures().triggerProps.onKeyDown(syntheticEvent({ type: 'keydown', key: 'Enter', currentTarget: row }))
  assert.deepEqual(h.opened.map(entry => entry.options), [
    { focusFirstAction: true },
    { focusFirstAction: true },
  ])
  assert.equal(h.opened[0].trigger, row)
})

test('a stationary touch hold opens the menu once and its release does not open the item', t => {
  const h = harness(t)
  const { triggerProps } = h.gestures()
  triggerProps.onPointerDown(syntheticEvent({
    pointerType: 'touch', button: 0, clientX: 40, clientY: 60, currentTarget: row,
  }))
  h.advance(PRESS_MENU_HOLD_MS - 1)
  assert.equal(h.opened.length, 0, 'a shorter hold stays a tap')
  h.advance(1)
  assert.deepEqual(h.opened, [{ point: { x: 40, y: 60 }, trigger: row, options: undefined }])
  triggerProps.onPointerUp()
  assert.equal(h.gestures().consumeHeldClick(), true, 'the release click is consumed')
  assert.equal(h.gestures().consumeHeldClick(), false, 'only that one click is consumed')
})

test('moving a held touch scrolls instead of opening the menu', t => {
  const h = harness(t)
  const { triggerProps } = h.gestures()
  triggerProps.onPointerDown(syntheticEvent({
    pointerType: 'touch', button: 0, clientX: 40, clientY: 60, currentTarget: row,
  }))
  triggerProps.onPointerMove({ clientX: 40, clientY: 60 + PRE_HOLD_MOVE_PX + 1 })
  h.advance(PRESS_MENU_HOLD_MS)
  assert.equal(h.opened.length, 0)
  assert.equal(h.gestures().consumeHeldClick(), false)
})

test('the native touch contextmenu cannot pre-empt the hold', t => {
  const h = harness(t)
  const { triggerProps } = h.gestures()
  triggerProps.onPointerDown(syntheticEvent({
    pointerType: 'touch', button: 0, clientX: 40, clientY: 60, currentTarget: row,
  }))
  const native = syntheticEvent({
    type: 'contextmenu', clientX: 40, clientY: 60, currentTarget: row,
    nativeEvent: { pointerType: '', stopImmediatePropagation() {} },
  })
  triggerProps.onContextMenuCapture(native)
  triggerProps.onContextMenu(native)
  assert.equal(native.defaultPrevented, true)
  assert.equal(h.opened.length, 0)
})

test('a right-click opens on release at the press point, and its contextmenu does not open twice', t => {
  const h = harness(t)
  const { triggerProps } = h.gestures()
  triggerProps.onPointerDown(syntheticEvent({
    pointerType: 'mouse', button: 2, pointerId: 7, clientX: 12, clientY: 34, currentTarget: row,
  }))
  triggerProps.onContextMenu(syntheticEvent({
    type: 'contextmenu', clientX: 12, clientY: 34, currentTarget: row,
    nativeEvent: { pointerType: 'mouse' },
  }))
  assert.equal(h.opened.length, 0, 'secondary-button down does not open yet')
  h.fireWindow('pointerup', syntheticEvent({ pointerId: 7, button: 2 }))
  assert.deepEqual(h.opened, [{ point: { x: 12, y: 34 }, trigger: row, options: undefined }])
  h.fireWindow('pointerup', syntheticEvent({ pointerId: 7, button: 2 }))
  assert.equal(h.opened.length, 1)
})

test('unmounting cancels a pending hold', t => {
  const h = harness(t)
  h.gestures().triggerProps.onPointerDown(syntheticEvent({
    pointerType: 'touch', button: 0, clientX: 1, clientY: 1, currentTarget: row,
  }))
  h.unmount()
  h.advance(PRESS_MENU_HOLD_MS)
  assert.equal(h.opened.length, 0)
})
