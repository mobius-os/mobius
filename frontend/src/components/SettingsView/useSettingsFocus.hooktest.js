import test from 'node:test'
import assert from 'node:assert/strict'
import { settingsFocusDestination } from './useSettingsFocus.js'

test('a page request opens that page, not the current model provider', () => {
  assert.deepEqual(settingsFocusDestination({ page: 'claude' }, 'codex'), {
    page: 'claude', anchor: null,
  })
  assert.deepEqual(settingsFocusDestination({ page: 'mobius' }, null), {
    page: 'mobius', anchor: null,
  })
})

test('section requests keep their existing destinations', () => {
  assert.deepEqual(settingsFocusDestination({ section: 'models' }, 'codex'), {
    page: 'codex', anchor: 'chat-models',
  })
  assert.deepEqual(settingsFocusDestination({ section: 'models' }, null), {
    page: null, anchor: 'ai-providers',
  })
  assert.deepEqual(settingsFocusDestination({ section: 'mobius-models' }, 'codex'), {
    page: 'mobius', anchor: null,
  })
  assert.deepEqual(settingsFocusDestination({ section: 'github' }, 'codex'), {
    page: null, anchor: 'github',
  })
})
