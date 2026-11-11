import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

// The one rename field shared by drawer rows, launcher cards, and project chat
// rows. It runs here as a component function so its focus, key, composition,
// outside pointer, and app focus rules are exercised directly.
const shimPath = fileURLToPath(new URL('../../ChatView/hooks/__tests__/react-hook-shim.mjs', import.meta.url))
const vite = await createSsrRenderServer({
  plugins: [{
    name: 'rename-test-hooks',
    enforce: 'pre',
    transform(code, id) {
      if (id.endsWith('/InlineRenameInput.jsx')) {
        return code.replace("from 'react'", `from ${JSON.stringify(shimPath)}`)
      }
    },
  }],
})
const { renderHook } = await vite.ssrLoadModule(shimPath)
const { default: InlineRenameInput } = await vite.ssrLoadModule('/src/components/Drawer/InlineRenameInput.jsx')
after(() => vite.close())

function editor(t) {
  const frames = new Map()
  let frameId = 0
  const document = new EventTarget()
  document.body = {}
  document.documentElement = {}
  document.activeElement = document.body
  document.visibilityState = 'visible'
  const window = new EventTarget()
  let pointerTarget = 'outside'
  const restoreGlobals = []
  for (const [name, value] of Object.entries({
    document,
    window,
    requestAnimationFrame: callback => { frames.set(++frameId, callback); return frameId },
    cancelAnimationFrame: id => frames.delete(id),
  })) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, value })
    restoreGlobals.push(() => {
      if (original) Object.defineProperty(globalThis, name, original)
      else delete globalThis[name]
    })
  }
  const submitted = []
  let cancelled = 0
  let focusCount = 0
  const hook = renderHook(() => InlineRenameInput({
    initialValue: 'Original',
    onSubmit: title => submitted.push(title),
    onCancel: () => { cancelled++ },
  }))
  t.after(() => {
    hook.unmount()
    for (const restore of restoreGlobals) restore()
  })
  const element = hook.result.current
  const input = {
    value: 'Original',
    isConnected: true,
    focus() { focusCount++; document.activeElement = input },
    select() {},
    contains: () => pointerTarget === 'inside',
  }
  element.props.ref.current = input
  function flushFrames() {
    const pending = [...frames.values()]
    frames.clear()
    for (const callback of pending) callback()
  }
  flushFrames()
  function dispatch(type, target) {
    pointerTarget = target
    const event = new Event(type, { cancelable: true })
    document.dispatchEvent(event)
    return event
  }
  function typed(value) {
    input.value = value
    element.props.onChange?.({ currentTarget: input })
  }
  return {
    submitted,
    document,
    focused: () => input,
    focusCount: () => focusCount,
    cancelled: () => cancelled,
    change: typed,
    compose(value) {
      element.props.onCompositionStart()
      typed(value)
    },
    endComposition(value) {
      typed(value)
      element.props.onCompositionEnd()
    },
    leaveApp() { window.dispatchEvent(new Event('blur')) },
    hidePage() {
      document.visibilityState = 'hidden'
      document.dispatchEvent(new Event('visibilitychange'))
    },
    blur(target = document.body) {
      document.activeElement = target
      element.props.onBlur()
      flushFrames()
    },
    pointerDown(target = 'outside') { return dispatch('pointerdown', target) },
    click(target = 'outside') { return dispatch('click', target) },
    key(key, nativeEvent = {}) {
      input.value = '  新しい名前  '
      const event = {
        key, nativeEvent, currentTarget: input,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true },
      }
      element.props.onKeyDown(event)
      return event
    },
  }
}

for (const key of ['Enter', 'Escape']) {
  test(`composing ${key} leaves rename active until a later Enter saves the completed name`, t => {
    const h = editor(t)
    assert.equal(h.key(key, { isComposing: true }).defaultPrevented, false)
    assert.deepEqual(h.submitted, [])
    assert.equal(h.cancelled(), 0)
    assert.equal(h.key('Enter').defaultPrevented, true)
    assert.deepEqual(h.submitted, ['新しい名前'])
    h.key('Enter')
    assert.equal(h.submitted.length, 1)
  })
}

test('ordinary Escape still cancels rename without saving', t => {
  const h = editor(t)
  assert.equal(h.key('Escape').defaultPrevented, true)
  assert.equal(h.cancelled(), 1)
  assert.deepEqual(h.submitted, [])
})

test('an unchanged or blank name leaves the item as it was', t => {
  for (const value of ['  Original ', '   ']) {
    const h = editor(t)
    h.change(value)
    h.blur({})
    assert.deepEqual(h.submitted, [])
    assert.equal(h.cancelled(), 1)
  }
})

test('a tap outside saves the typed name and still reaches what it landed on', t => {
  const h = editor(t)
  h.change('Edited title')
  assert.equal(h.pointerDown().defaultPrevented, false)
  assert.deepEqual(h.submitted, ['Edited title'])
  assert.equal(h.click().defaultPrevented, false)
  h.blur()
  assert.deepEqual(h.submitted, ['Edited title'], 'the following blur saves nothing twice')
  assert.equal(h.cancelled(), 0)
})

test('a tap on the drawer scrim saves the typed name before the drawer closes', t => {
  const h = editor(t)
  h.change('Edited title')
  assert.equal(h.pointerDown('scrim').defaultPrevented, false)
  assert.deepEqual(h.submitted, ['Edited title'])
  assert.equal(h.click('scrim').defaultPrevented, false)
})

test('a tap outside with an unchanged or blank name saves nothing', t => {
  for (const value of ['Original', '  ']) {
    const h = editor(t)
    h.change(value)
    h.pointerDown()
    assert.deepEqual(h.submitted, [])
    assert.equal(h.cancelled(), 1)
  }
})

test('switching to another window saves the typed name while the field keeps focus', t => {
  const h = editor(t)
  h.change('Edited title')
  h.blur(h.focused())
  assert.deepEqual(h.submitted, [], 'a blur that leaves the field active is not a leave')
  h.leaveApp()
  assert.deepEqual(h.submitted, ['Edited title'])
})

test('a page hidden by an app switch saves the typed name', t => {
  const h = editor(t)
  h.change('Edited title')
  h.hidePage()
  assert.deepEqual(h.submitted, ['Edited title'])
})

for (const [leave, how] of [
  ['tap outside', h => h.pointerDown()],
  ['field blur', h => h.blur({})],
  ['window switch', h => h.leaveApp()],
]) {
  test(`a ${leave} mid-composition saves the finished text only once composition ends`, t => {
    const h = editor(t)
    h.compose('しんきかく')
    how(h)
    assert.deepEqual(h.submitted, [])
    assert.equal(h.cancelled(), 0)
    h.endComposition('新企画')
    assert.deepEqual(h.submitted, ['新企画'])
  })
}

test('Safari composition-confirming Enter (keyCode 229) does not save', t => {
  const h = editor(t)
  h.key('Enter', { keyCode: 229 })
  assert.deepEqual(h.submitted, [])
  h.key('Enter')
  assert.deepEqual(h.submitted, ['新しい名前'])
})

test('menu teardown may recover focus once; a later body blur settles the edit', t => {
  const h = editor(t)
  h.blur()
  assert.equal(h.focusCount(), 2)
  assert.deepEqual(h.submitted, [])
  assert.equal(h.cancelled(), 0)
  h.blur(h.document.documentElement)
  assert.equal(h.focusCount(), 2)
  assert.equal(h.cancelled(), 1)
})

test('a tap inside the field keeps editing and ends the menu focus handoff', t => {
  const h = editor(t)
  h.pointerDown('inside')
  assert.equal(h.cancelled(), 0)
  h.change('Edited title')
  h.blur()
  assert.deepEqual(h.submitted, ['Edited title'])
  assert.equal(h.focusCount(), 1)
})

test('keyboard and input activity end the menu focus handoff before a body blur', t => {
  const h = editor(t)
  h.key('Tab')
  h.blur()
  assert.deepEqual(h.submitted, ['新しい名前'])
  assert.equal(h.focusCount(), 1)
})

test('editing without a pointer or key also ends menu focus recovery', t => {
  const h = editor(t)
  h.change('Dictated title')
  h.blur()
  assert.deepEqual(h.submitted, ['Dictated title'])
  assert.equal(h.focusCount(), 1)
})

test('tabbing to a focusable control saves without stealing its focus', t => {
  const h = editor(t)
  h.change('Edited title')
  const destination = {}
  h.blur(destination)
  assert.deepEqual(h.submitted, ['Edited title'])
  assert.equal(h.document.activeElement, destination)
})
