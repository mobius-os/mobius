import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  _resetDisclosureStateForTests,
  disclosureIsOpen,
  persistDisclosureOpen,
} from '../disclosureState.js'

function memorySessionStorage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  }
}

test('disclosure screen state restores per chat and stable surface key', () => {
  const original = globalThis.sessionStorage
  globalThis.sessionStorage = memorySessionStorage()
  _resetDisclosureStateForTests()
  try {
    persistDisclosureOpen('chat-a', 'message-1:activity:think-1', true)
    assert.equal(disclosureIsOpen('chat-a', 'message-1:activity:think-1'), true)
    assert.equal(disclosureIsOpen('chat-a', 'message-2:activity:think-1'), false)
    assert.equal(disclosureIsOpen('chat-b', 'message-1:activity:think-1'), false)

    // Simulate a ChatView remount by dropping the module's memory cache. The
    // browser-session copy remains the restoration authority.
    _resetDisclosureStateForTests()
    assert.equal(disclosureIsOpen('chat-a', 'message-1:activity:think-1'), true)

    persistDisclosureOpen('chat-a', 'message-1:activity:think-1', false)
    _resetDisclosureStateForTests()
    assert.equal(disclosureIsOpen('chat-a', 'message-1:activity:think-1'), false)
  } finally {
    _resetDisclosureStateForTests()
    globalThis.sessionStorage = original
  }
})
