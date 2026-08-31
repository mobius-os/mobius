import test from 'node:test'
import assert from 'node:assert/strict'

import {
  artifactRelatedToApps,
  appArtifactsFromBuiltApps,
  artifactTouchForChat,
  artifactsTouchedByChat,
  chatArtifactPickerItems,
  loadChatArtifacts,
} from '../chatArtifacts.js'

test('built apps become durable chat artifacts without duplicate rows', () => {
  const apps = appArtifactsFromBuiltApps([
    { id: 7, name: 'Atlas', updated_at: '2026-08-20T10:00:00Z' },
    { id: 9, name: 'Canvas', updated_at: '2026-08-21T10:00:00Z' },
    { id: 7, name: 'Atlas refined', updated_at: '2026-08-22T10:00:00Z' },
  ])
  assert.deepEqual(apps.map(app => app.name), ['Atlas refined', 'Canvas'])
})

test('artifact picker orders apps and documents together by their chat touch', () => {
  const items = chatArtifactPickerItems([
    {
      id: 7,
      name: 'Atlas',
      chat_touched_at: '2026-08-22T10:00:00Z',
      has_unseen_chat_update: true,
    },
    { id: 9, name: 'Canvas', chat_touched_at: '2026-08-20T10:00:00Z' },
  ], [
    {
      id: 'launch-brief',
      title: 'Launch brief',
      touchedAt: '2026-08-21T10:00:00Z',
      version: 2,
    },
  ])

  assert.deepEqual(items.map(item => item.key), [
    'app:7',
    'artifact:launch-brief',
    'app:9',
  ])
  assert.equal(items[0].title, 'Atlas')
  assert.equal(items[0].unseen, true)
})

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

test('artifacts related to an app appear in its maintaining chat only', () => {
  const record = {
    id: 'store-concepts-a690',
    title: 'Store concepts',
    chat_id: 'origin-chat',
    created_at: '2026-08-23T14:08:00Z',
    updated_at: '2026-08-23T14:26:00Z',
    current_version: 5,
    related_apps: [{ id: 39, slug: 'app-store', name: 'App Store' }],
    versions: [
      { v: 1, chat_id: 'origin-chat', created_at: '2026-08-23T14:08:00Z' },
      { v: 5, chat_id: 'origin-chat', created_at: '2026-08-23T14:26:00Z' },
    ],
  }

  const appChatTouch = artifactTouchForChat(record, 'app-maintaining-chat', [
    { id: 39, slug: 'app-store' },
  ])
  assert.equal(appChatTouch?.id, 'store-concepts-a690')
  assert.equal(appChatTouch?.version, 5)
  assert.equal(appChatTouch?.touchedAt, '2026-08-23T14:26:00Z')
  assert.equal(
    artifactTouchForChat(record, 'unrelated-chat', [{ id: 12, slug: 'notes' }]),
    null,
  )
  assert.equal(artifactTouchForChat(record, 'origin-chat')?.version, 5)
})

test('related app matching treats the stable slug as authoritative', () => {
  const record = {
    related_apps: [{ id: 39, slug: 'app-store' }],
  }
  assert.equal(artifactRelatedToApps(record, [{ id: 104, slug: 'app-store' }]), true)
  assert.equal(artifactRelatedToApps(record, [{ id: 39, slug: 'renamed-store' }]), false)
  assert.equal(artifactRelatedToApps({ related_apps: [{ id: 39 }] }, [
    { id: 39, slug: 'app-store' },
  ]), true)
  assert.equal(artifactRelatedToApps({ related_apps: ['app-store'] }, [
    { id: 39, slug: 'app-store' },
  ]), false)
})

test('artifact loading resolves related apps from the durable chat relation', async () => {
  const calls = []
  const artifact = {
    id: 'store-concepts-a690',
    title: 'Store concepts',
    chat_id: 'origin-chat',
    updated_at: '2026-08-23T14:26:00Z',
    current_version: 5,
    related_apps: [{ id: 39, slug: 'app-store' }],
  }
  const request = async (path) => {
    calls.push(path)
    return {
      ok: true,
      json: async () => ({ entries: [{ content: artifact }], next_cursor: null }),
    }
  }

  const loaded = await loadChatArtifacts(88, 'app-chat', {
    request,
    relatedApps: [{ id: 39, slug: 'app-store' }],
  })
  assert.deepEqual(loaded.map(item => item.id), ['store-concepts-a690'])
  assert.deepEqual(calls, [
    '/storage/apps-list/88/artifacts/?limit=500&include_content=true',
  ])
})

test('artifact loading keeps origin provenance when no related apps are supplied', async () => {
  const request = async (path) => {
    return {
      ok: true,
      json: async () => ({
        entries: [
          { content: { id: 'origin-artifact', chat_id: 'app-chat' } },
          {
            content: {
              id: 'related-artifact',
              chat_id: 'other-chat',
              related_apps: [{ slug: 'app-store' }],
            },
          },
        ],
        next_cursor: null,
      }),
    }
  }

  const loaded = await loadChatArtifacts(88, 'app-chat', { request })
  assert.deepEqual(loaded.map(item => item.id), ['origin-artifact'])
})
