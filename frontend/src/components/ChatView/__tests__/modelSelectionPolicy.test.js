import test from 'node:test'
import assert from 'node:assert/strict'

import { needsModelSelection } from '../modelSelectionPolicy.js'


test('interactive chats require a model before sending', () => {
  assert.equal(needsModelSelection({
    showPicker: true,
    chatInfo: { effective: { model: null } },
  }), true)
})

test('a cold activation defers unknown model state to the authoritative send guard', () => {
  assert.equal(needsModelSelection({
    showPicker: true,
    chatInfo: null,
  }), false)
})

test('an explicit effective model lets the composer send', () => {
  assert.equal(needsModelSelection({
    showPicker: true,
    chatInfo: { effective: { model: 'gpt-5.6-sol' } },
  }), false)
})

test('app embeds that intentionally hide the picker retain their configured send path', () => {
  assert.equal(needsModelSelection({
    showPicker: false,
    chatInfo: { effective: { model: null } },
  }), false)
})
