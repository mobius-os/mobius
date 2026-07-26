import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  RECENT_CHAT_PREFETCH_LIMIT,
  recentChatsToPrefetch,
} from '../chatPrefetch.js'

test('recent chat prefetch stays bounded, skips active/empty, and keeps running chats', () => {
  const chats = [
    { id: 'active', has_messages: true, activity_at: '2026-07-26T10:00:00Z' },
    { id: 'running', has_messages: true, running: true, activity_at: '2026-07-26T12:00:00Z' },
    { id: 'empty', has_messages: false, activity_at: '2026-07-26T13:00:00Z' },
    { id: 'older', has_messages: true, activity_at: '2026-07-25T10:00:00Z' },
    { id: 'newer', has_messages: true, activity_at: '2026-07-26T09:00:00Z' },
    { id: 'middle', has_messages: true, activity_at: '2026-07-26T08:00:00Z' },
  ]

  const selected = recentChatsToPrefetch(chats, 'active')

  assert.equal(selected.length, RECENT_CHAT_PREFETCH_LIMIT)
  assert.deepEqual(selected.map(chat => chat.id), ['running', 'newer'])
})
