import assert from 'node:assert/strict'
import test from 'node:test'

import {
  beginProviderSwitch,
  completeProviderSwitch,
  getProviderSwitchState,
  resetProviderSwitchMemoryForTests,
  stageProviderSwitch,
} from '../../providerSwitch.js'
import useDiscardUnconfirmedSwitchOnPickerClose from '../useDiscardUnconfirmedSwitchOnPickerClose.js'
import { renderHook } from './react-hook-shim.mjs'

// Renders the hook inside the unified open picker, then returns a close action
// that drives the true -> false transition the component sees on dismissal.
function openPicker(chatId, status) {
  const { rerender } = renderHook(
    useDiscardUnconfirmedSwitchOnPickerClose,
    true,
    status,
    chatId,
  )
  return {
    closePicker: () => rerender(false, status, chatId),
    rerender,
  }
}

test('closing the picker on an unconfirmed switch discards it', () => {
  resetProviderSwitchMemoryForTests()
  const chatId = 'chat-confirming'
  stageProviderSwitch(chatId, { chatId, switchId: 'switch-1' })
  assert.equal(getProviderSwitchState(chatId).status, 'confirming')

  const { closePicker } = openPicker(chatId, 'confirming')
  closePicker()

  assert.equal(getProviderSwitchState(chatId).status, 'idle')
})

test('closing the picker leaves an in-flight switch untouched', () => {
  resetProviderSwitchMemoryForTests()
  const chatId = 'chat-switching'
  const request = { chatId, switchId: 'switch-1' }
  stageProviderSwitch(chatId, request)
  beginProviderSwitch(chatId, request)
  assert.equal(getProviderSwitchState(chatId).status, 'switching')

  const { closePicker } = openPicker(chatId, 'switching')
  closePicker()

  assert.equal(getProviderSwitchState(chatId).status, 'switching')
})

test('closing the picker leaves a committed switch untouched', () => {
  resetProviderSwitchMemoryForTests()
  const chatId = 'chat-success'
  const request = { chatId, switchId: 'switch-1' }
  completeProviderSwitch(chatId, request, { provider: 'codex' })
  assert.equal(getProviderSwitchState(chatId).status, 'success')

  const { closePicker } = openPicker(chatId, 'success')
  closePicker()

  assert.equal(getProviderSwitchState(chatId).status, 'success')
})

test('closing the picker with no staged switch is a no-op', () => {
  resetProviderSwitchMemoryForTests()
  const chatId = 'chat-idle'

  const { closePicker } = openPicker(chatId, undefined)
  closePicker()

  assert.equal(getProviderSwitchState(chatId).status, 'idle')
})

test('opening the picker does not discard a confirming switch', () => {
  resetProviderSwitchMemoryForTests()
  const chatId = 'chat-open'
  stageProviderSwitch(chatId, { chatId, switchId: 'switch-1' })

  // Start with the picker closed, then open it (false -> true). Entering the
  // picker is not a close, so the staged switch must survive to be shown.
  const { rerender } = renderHook(
    useDiscardUnconfirmedSwitchOnPickerClose,
    false,
    'confirming',
    chatId,
  )
  rerender(true, 'confirming', chatId)

  assert.equal(getProviderSwitchState(chatId).status, 'confirming')
})
