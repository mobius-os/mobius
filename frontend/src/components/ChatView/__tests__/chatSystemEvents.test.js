import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isChatStreamSystemEvent,
  shouldForwardChatStreamSystemEvent,
} from '../chatSystemEvents.js'

test('chat stream recognizes catch-up-safe system events without swallowing unknown events', () => {
  assert.equal(isChatStreamSystemEvent('theme_updated'), true)
  assert.equal(isChatStreamSystemEvent('app_updated'), true)
  assert.equal(isChatStreamSystemEvent('build_phase'), true)
  assert.equal(isChatStreamSystemEvent('text'), false)
})

test('catch-up-unsafe events never ride the chat stream (system-bus-only)', () => {
  // These are published to the system broadcast alone, so they must not be
  // recognized as chat-stream events at all — there is nothing to gate.
  for (const type of [
    'shell_rebuilding',
    'shell_rebuilt',
    'shell_apply_now',
    'shell_rebuild_failed',
    'app_build_failed',
    'app_built',
  ]) {
    assert.equal(isChatStreamSystemEvent(type), false, `${type} is system-bus-only`)
    assert.equal(shouldForwardChatStreamSystemEvent({ type }), false)
  }
})

test('every recognized chat-stream system event forwards (all are catch-up-safe)', () => {
  for (const type of ['theme_updated', 'app_updated', 'build_phase', 'chat_run_started', 'chat_run_finished']) {
    assert.equal(
      shouldForwardChatStreamSystemEvent({ type }),
      true,
      `${type} should forward`,
    )
  }
})
