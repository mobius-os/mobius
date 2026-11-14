import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import useImmersiveStateEcho from '../useImmersiveStateEcho.js'

function renderEcho(initial) {
  const echoed = []
  const hook = renderHook(state => useImmersiveStateEcho(state, () => echoed.push(state)), initial)
  return { echoed, rerender: next => hook.rerender(next), unmount: hook.unmount }
}

test('a request the shell holds is still answered with the unchanged verdict', () => {
  // An app in a split pane asks for immersive. Its runtime already flipped
  // `.hidden` optimistically; the shell holds the request, so the verdict
  // stays null and only the per-request echo can correct the app.
  const hook = renderEcho({ immersiveMode: null, liveVersion: 1, requestCount: 0 })
  hook.echoed.length = 0

  hook.rerender({ immersiveMode: null, liveVersion: 1, requestCount: 1 })
  assert.equal(hook.echoed.length, 1, 'the held request is answered')
  assert.equal(hook.echoed[0].immersiveMode, null)

  hook.rerender({ immersiveMode: null, liveVersion: 1, requestCount: 2 })
  assert.equal(hook.echoed.length, 2, 'every later request is answered too')
  hook.unmount()
})

test('a verdict flip and a promoted frame each get the verdict', () => {
  const hook = renderEcho({ immersiveMode: null, liveVersion: 1, requestCount: 0 })
  assert.equal(hook.echoed.length, 1, 'a mounted canvas echoes once')

  hook.rerender({ immersiveMode: 'full', liveVersion: 1, requestCount: 0 })
  assert.equal(hook.echoed.at(-1).immersiveMode, 'full')

  hook.rerender({ immersiveMode: 'full', liveVersion: 2, requestCount: 0 })
  assert.equal(hook.echoed.at(-1).liveVersion, 2)
  assert.equal(hook.echoed.length, 3)
  hook.unmount()
})

test('an unrelated re-render does not echo again', () => {
  const hook = renderEcho({ immersiveMode: 'full', liveVersion: 1, requestCount: 3 })
  hook.rerender({ immersiveMode: 'full', liveVersion: 1, requestCount: 3 })
  assert.equal(hook.echoed.length, 1)
  hook.unmount()
})
