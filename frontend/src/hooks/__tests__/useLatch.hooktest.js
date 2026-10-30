import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import useLatch from '../useLatch.js'

test('a surface that was never shown stays unmounted', () => {
  const hook = renderHook(shown => useLatch(shown), false)
  hook.rerender(false)
  assert.equal(hook.result.current, false)
  hook.unmount()
})

test('a surface stays mounted after it is first shown and then hidden', () => {
  const hook = renderHook(shown => useLatch(shown), false)

  hook.rerender(true)
  assert.equal(hook.result.current, true, 'mounts on the first render it is shown')

  hook.rerender(false)
  assert.equal(hook.result.current, true, 'hiding it again keeps its state mounted')
  hook.unmount()
})

test('a surface shown at launch mounts on the first render', () => {
  const hook = renderHook(shown => useLatch(shown), true)
  assert.equal(hook.result.current, true)
  hook.unmount()
})
