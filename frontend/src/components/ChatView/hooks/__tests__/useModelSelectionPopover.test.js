import test from 'node:test'
import assert from 'node:assert/strict'

import { renderHook } from './react-hook-shim.mjs'
import useModelSelectionPopover from '../useModelSelectionPopover.js'


test('a later model-selection request opens and can reopen the popover', () => {
  const input = {}
  const composerInputRef = { current: input }
  globalThis.document = { activeElement: input }
  const hook = renderHook(
    useModelSelectionPopover,
    0,
    composerInputRef,
  )

  assert.equal(hook.result.current.mode, null)
  hook.rerender(1, composerInputRef)
  assert.equal(hook.result.current.mode, 'model')
  assert.equal(hook.result.current.wasInputFocusedRef.current, true)

  hook.result.current.setMode(null)
  assert.equal(hook.result.current.mode, null)
  hook.rerender(2, composerInputRef)
  assert.equal(hook.result.current.mode, 'model')

  delete globalThis.document
})


test('a nonzero initial request does not open on mount', () => {
  globalThis.document = { activeElement: null }
  const hook = renderHook(
    useModelSelectionPopover,
    4,
    { current: {} },
  )

  assert.equal(hook.result.current.mode, null)
  delete globalThis.document
})

test('composer option triggers switch one shared mode instead of opening peers', () => {
  globalThis.document = { activeElement: null }
  const hook = renderHook(useModelSelectionPopover, 0, { current: {} })

  hook.result.current.setMode('options')
  assert.equal(hook.result.current.mode, 'options')
  hook.result.current.setMode('model')
  assert.equal(hook.result.current.mode, 'model')
  hook.result.current.setMode(null)
  assert.equal(hook.result.current.mode, null)

  delete globalThis.document
})
