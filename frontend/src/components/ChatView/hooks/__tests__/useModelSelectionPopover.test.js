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

  assert.equal(hook.result.current.open, false)
  hook.rerender(1, composerInputRef)
  assert.equal(hook.result.current.open, true)
  assert.equal(hook.result.current.wasInputFocusedRef.current, true)

  hook.result.current.setOpen(false)
  assert.equal(hook.result.current.open, false)
  hook.rerender(2, composerInputRef)
  assert.equal(hook.result.current.open, true)

  delete globalThis.document
})


test('a nonzero initial request does not open on mount', () => {
  globalThis.document = { activeElement: null }
  const hook = renderHook(
    useModelSelectionPopover,
    4,
    { current: {} },
  )

  assert.equal(hook.result.current.open, false)
  delete globalThis.document
})
