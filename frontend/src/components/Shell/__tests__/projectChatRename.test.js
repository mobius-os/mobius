import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const shimPath = fileURLToPath(new URL('../../ChatView/hooks/__tests__/react-hook-shim.mjs', import.meta.url))
const vite = await createSsrRenderServer({
  plugins: [{
    name: 'rename-test-hooks',
    enforce: 'pre',
    transform(code, id) {
      if (id.endsWith('/ProjectChatList.jsx')) {
        return code.replace("from 'react'", `from ${JSON.stringify(shimPath)}`)
      }
    },
  }],
})
const { renderHook } = await vite.ssrLoadModule(shimPath)
const { ProjectChatRename } = await vite.ssrLoadModule('/src/components/Projects/ProjectChatList.jsx')
after(() => vite.close())

function editor(t) {
  const frames = new Map()
  let frameId = 0
  const document = new EventTarget()
  document.body = {}
  document.documentElement = {}
  document.activeElement = document.body
  const restoreGlobals = []
  for (const [name, value] of Object.entries({
    document,
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
  const hook = renderHook(() => ProjectChatRename({
    chat: { title: 'Original' },
    onSubmit: title => submitted.push(title),
    onCancel: () => { cancelled++ },
  }))
  t.after(() => {
    hook.unmount()
    for (const restore of restoreGlobals) restore()
  })
  const element = hook.result.current.props.children[1]
  const input = {
    value: 'Original',
    isConnected: true,
    focus() { focusCount++; document.activeElement = input },
    select() {},
  }
  element.props.ref.current = input
  function flushFrames() {
    const pending = [...frames.values()]
    frames.clear()
    for (const callback of pending) callback()
  }
  flushFrames()
  return {
    submitted,
    document,
    focusCount: () => focusCount,
    cancelled: () => cancelled,
    change(value) {
      input.value = value
      element.props.onChange?.({ currentTarget: input })
    },
    blur(target = document.body) {
      document.activeElement = target
      element.props.onBlur()
      flushFrames()
    },
    pointerDown() { document.dispatchEvent(new Event('pointerdown')) },
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


test('clicking blank space after editing saves instead of reclaiming focus', t => {
  const h = editor(t)
  h.change('Edited title')
  h.pointerDown()
  h.blur()
  assert.deepEqual(h.submitted, ['Edited title'])
  assert.equal(h.focusCount(), 1)
})

test('menu teardown may recover focus once, but later blank-space clicks save', t => {
  const h = editor(t)
  h.blur()
  assert.equal(h.focusCount(), 2)
  assert.deepEqual(h.submitted, [])
  assert.equal(h.cancelled(), 0)
  h.change('Edited title')
  h.pointerDown()
  h.blur(h.document.documentElement)
  assert.deepEqual(h.submitted, ['Edited title'])
  assert.equal(h.focusCount(), 2)
})

test('the first outside pointer ends focus recovery even without a menu-teardown blur', t => {
  const h = editor(t)
  h.pointerDown()
  h.blur()
  assert.equal(h.cancelled(), 1)
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

test('a focusable outside destination settles without stealing its focus', t => {
  const h = editor(t)
  h.change('Edited title')
  const destination = {}
  h.blur(destination)
  assert.deepEqual(h.submitted, ['Edited title'])
  assert.equal(h.document.activeElement, destination)
})


test('focus recovery is consumed by the first menu-teardown blur', t => {
  const h = editor(t)
  h.blur()
  h.blur()
  assert.equal(h.focusCount(), 2)
  assert.equal(h.cancelled(), 1)
})
