import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  chatAppArtifactInvalidation,
  projectChatAppArtifacts,
} from '../chatAppArtifactState.js'

const row = (id, touchedAt, seenAt = null) => ({
  app: { id, name: `App ${id}`, updated_at: `bundle-${id}` },
  touched_at: touchedAt,
  seen_at: seenAt,
})

test('chat app artifacts preserve their independent touch and bundle versions', () => {
  assert.deepEqual(projectChatAppArtifacts([
    row(7, '2026-08-29T10:00:00Z'),
  ]), [{
    id: 7,
    name: 'App 7',
    updated_at: 'bundle-7',
    chat_touched_at: '2026-08-29T10:00:00Z',
    chat_seen_at: null,
    has_unseen_chat_update: true,
  }])
})

test('only an exact Brain acknowledgement clears a touch cursor', () => {
  const touchedAt = '2026-08-29T10:00:00Z'
  assert.equal(
    projectChatAppArtifacts([row(7, touchedAt, touchedAt)])[0]
      .has_unseen_chat_update,
    false,
  )
  assert.equal(
    projectChatAppArtifacts([row(7, touchedAt, '2026-08-29T09:00:00Z')])[0]
      .has_unseen_chat_update,
    true,
  )
})

test('artifact projection is oldest-first and excludes malformed rows', () => {
  assert.deepEqual(projectChatAppArtifacts([
    row(9, '2026-08-29T12:00:00Z'),
    { touched_at: '2026-08-29T11:00:00Z' },
    row(7, '2026-08-29T10:00:00Z'),
  ]).map(app => app.id), [7, 9])
})

test('empty chat artifact projections reuse one frozen value', () => {
  assert.equal(projectChatAppArtifacts([]), projectChatAppArtifacts(null))
})

test('app identity updates refresh artifacts but opening an app does not', () => {
  assert.deepEqual(chatAppArtifactInvalidation({ type: 'app_updated' }), {
    scope: 'all',
  })
  assert.equal(chatAppArtifactInvalidation({ type: 'app_opened' }), null)
})

test('artifact lifecycle invalidation follows the owning event scope', () => {
  assert.deepEqual(chatAppArtifactInvalidation({ type: 'app_deleted' }), {
    scope: 'all',
  })
  assert.deepEqual(chatAppArtifactInvalidation({ type: 'app_recovered' }), {
    scope: 'all',
  })
  assert.deepEqual(chatAppArtifactInvalidation({
    type: 'app_preview_ready',
    chatId: 'chat-7',
  }), {
    scope: 'chat',
    chatId: 'chat-7',
  })
  assert.equal(chatAppArtifactInvalidation({
    type: 'app_preview_ready',
  }), null)
})
