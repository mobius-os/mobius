import test from 'node:test'
import assert from 'node:assert/strict'
import { settingsFocusDestination } from './useSettingsFocus.js'

test('model provider setup target opens the requested provider, not the current model provider', () => {
  assert.deepEqual(settingsFocusDestination('provider:claude', 'codex'), {
    page: 'claude', anchor: null,
  })
  assert.deepEqual(settingsFocusDestination('provider:codex', 'claude'), {
    page: 'codex', anchor: null,
  })
  assert.deepEqual(settingsFocusDestination('provider:mobius', null), {
    page: 'mobius', anchor: null,
  })
  assert.deepEqual(settingsFocusDestination('models', 'codex'), {
    page: 'codex', anchor: 'chat-models',
  })
})
