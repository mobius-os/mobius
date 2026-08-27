import test from 'node:test'
import assert from 'node:assert/strict'

import {
  needsModelSelection,
  resolvedChatSettings,
  selectedChatModel,
} from '../modelSelectionPolicy.js'


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

test('a retained cache cannot hide the explicit per-chat model', () => {
  const chatInfo = {
    agent_settings_json: { model: 'gpt-5.6-sol', effort: 'high' },
    effective: { model: '  ', effort: 'xhigh' },
  }
  assert.equal(selectedChatModel(chatInfo), 'gpt-5.6-sol')
  assert.deepEqual(resolvedChatSettings(chatInfo), {
    model: 'gpt-5.6-sol',
    effort: 'xhigh',
  })
  assert.equal(needsModelSelection({ showPicker: true, chatInfo }), false)
})

test('the effective model remains authoritative when both projections are valid', () => {
  assert.equal(selectedChatModel({
    agent_settings_json: { model: 'gpt-old' },
    effective: { model: 'gpt-current' },
  }), 'gpt-current')
})

test('app embeds that intentionally hide the picker retain their configured send path', () => {
  assert.equal(needsModelSelection({
    showPicker: false,
    chatInfo: { effective: { model: null } },
  }), false)
})
