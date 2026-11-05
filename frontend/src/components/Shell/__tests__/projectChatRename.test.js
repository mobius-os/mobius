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
  const restoreGlobals = []
  for (const [name, value] of Object.entries({ requestAnimationFrame: () => 1, cancelAnimationFrame: () => {} })) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name)
    Object.defineProperty(globalThis, name, { configurable: true, value })
    restoreGlobals.push(() => {
      if (original) Object.defineProperty(globalThis, name, original)
      else delete globalThis[name]
    })
  }
  const submitted = []
  let cancelled = 0
  const hook = renderHook(() => ProjectChatRename({
    chat: { title: 'Original' },
    onSubmit: title => submitted.push(title),
    onCancel: () => { cancelled++ },
  }))
  t.after(() => {
    hook.unmount()
    for (const restore of restoreGlobals) restore()
  })
  const input = hook.result.current.props.children[1]
  return {
    submitted,
    cancelled: () => cancelled,
    key(key, nativeEvent = {}) {
      const event = {
        key, nativeEvent, currentTarget: { value: '  新しい名前  ' },
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true },
      }
      input.props.onKeyDown(event)
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
