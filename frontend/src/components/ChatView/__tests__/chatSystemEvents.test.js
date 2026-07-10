import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isChatStreamSystemEvent,
  shouldForwardChatStreamSystemEvent,
} from '../chatSystemEvents.js'

test('chat stream recognizes system events without swallowing unknown events', () => {
  assert.equal(isChatStreamSystemEvent('theme_updated'), true)
  assert.equal(isChatStreamSystemEvent('shell_rebuilt'), true)
  assert.equal(isChatStreamSystemEvent('text'), false)
})

test('chat catch-up does not replay shell rebuild lifecycle events', () => {
  for (const type of ['shell_rebuilding', 'shell_rebuilt', 'shell_rebuild_failed']) {
    assert.equal(
      shouldForwardChatStreamSystemEvent({ type }, { isCatchUp: true }),
      false,
      `${type} should be ignored during chat catch-up`,
    )
    assert.equal(
      shouldForwardChatStreamSystemEvent({ type }, { isCatchUp: false }),
      true,
      `${type} should still forward when live`,
    )
  }
})

test('chat catch-up still forwards safe chat-scoped system events', () => {
  for (const type of ['theme_updated', 'app_updated', 'app_built', 'chat_run_started', 'chat_run_finished']) {
    assert.equal(
      shouldForwardChatStreamSystemEvent({ type }, { isCatchUp: true }),
      true,
      `${type} should remain catch-up-safe`,
    )
  }
})
