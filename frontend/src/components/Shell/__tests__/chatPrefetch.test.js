import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  CHAT_PREFETCH_LIMIT,
  warmChatCandidates,
} from '../chatPrefetch.js'

test('warm chats blend recent opens with owner activity and ignore agent-only churn', () => {
  const chats = [
    { id: 'active', has_messages: true, activity_at: '2026-07-26T10:00:00Z' },
    { id: 'opened-old', has_messages: true, activity_at: '2026-07-20T10:00:00Z' },
    { id: 'opened-empty', has_messages: false, activity_at: '2026-07-26T13:00:00Z' },
    { id: 'owner-1', has_messages: true, activity_at: '2026-07-26T12:00:00Z' },
    { id: 'owner-2', has_messages: true, activity_at: '2026-07-26T11:00:00Z' },
    { id: 'owner-3', has_messages: true, activity_at: '2026-07-26T10:00:00Z' },
    { id: 'owner-4', has_messages: true, activity_at: '2026-07-26T09:00:00Z' },
    { id: 'owner-5', has_messages: true, activity_at: '2026-07-26T08:00:00Z' },
    {
      id: 'agent-busy', has_messages: true,
      activity_at: '2026-07-01T08:00:00Z', updated_at: '2026-07-27T08:00:00Z',
    },
  ]

  const selected = warmChatCandidates(
    chats,
    'active',
    ['opened-old', 'owner-2', 'opened-empty', 'active'],
  )

  assert.equal(selected.length, CHAT_PREFETCH_LIMIT)
  assert.deepEqual(selected.map(chat => chat.id), [
    'opened-old', 'owner-2', 'owner-1', 'owner-3', 'owner-4', 'owner-5',
  ])
  assert.equal(selected.some(chat => chat.id === 'agent-busy'), false)
})

test('warm chats fall back to owner activity without local open history', () => {
  const chats = [
    { id: 'older', has_messages: true, activity_at: '2026-07-25T10:00:00Z' },
    { id: 'newer', has_messages: true, activity_at: '2026-07-26T09:00:00Z' },
  ]

  assert.deepEqual(
    warmChatCandidates(chats, null).map(chat => chat.id),
    ['newer', 'older'],
  )
})
