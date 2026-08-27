import test from 'node:test'
import assert from 'node:assert/strict'

import {
  artifactTouchForChat,
  artifactsTouchedByChat,
} from '../chatArtifacts.js'

test('chat artifacts are attributed by version touch, not global record recency', () => {
  const records = [
    {
      id: 'launch-brief-a1b2',
      title: 'Launch brief',
      chat_id: 'chat-a',
      created_at: '2026-08-20T10:00:00Z',
      updated_at: '2026-08-25T10:00:00Z',
      current_version: 3,
      versions: [
        { v: 1, chat_id: 'chat-a', created_at: '2026-08-20T10:00:00Z' },
        { v: 2, chat_id: 'chat-a', created_at: '2026-08-22T10:00:00Z' },
        { v: 3, chat_id: 'chat-b', created_at: '2026-08-25T10:00:00Z' },
      ],
    },
    {
      id: 'flow-map-c3d4',
      title: 'Flow map',
      chat_id: 'chat-b',
      created_at: '2026-08-21T10:00:00Z',
      updated_at: '2026-08-24T10:00:00Z',
      current_version: 2,
      versions: [
        { v: 1, chat_id: 'chat-b', created_at: '2026-08-21T10:00:00Z' },
        { v: 2, chat_id: 'chat-a', created_at: '2026-08-24T10:00:00Z' },
      ],
    },
  ]

  const touched = artifactsTouchedByChat(records, 'chat-a')
  assert.deepEqual(touched.map(item => item.id), ['flow-map-c3d4', 'launch-brief-a1b2'])
  assert.equal(touched[1].touchedAt, '2026-08-22T10:00:00Z')
  assert.equal(touched[1].version, 2)
  assert.equal(touched[0].version, 2)
})

test('legacy provenance is retained and malformed ids are ignored', () => {
  assert.equal(artifactTouchForChat({
    id: 'legacy-artifact',
    title: 'Legacy',
    chat_id: 'chat-a',
    created_at: '2026-08-20T10:00:00Z',
  }, 'chat-a')?.title, 'Legacy')
  assert.equal(artifactTouchForChat({
    id: '../escape', chat_id: 'chat-a',
  }, 'chat-a'), null)
})
