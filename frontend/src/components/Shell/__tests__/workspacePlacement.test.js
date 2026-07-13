import { test } from 'node:test'
import assert from 'node:assert/strict'

import { freshChatBuiltApps } from '../newAppAttention.js'
import { makeTab } from '../tabModel.js'
import {
  ACTIVATE_IN_BACKGROUND,
  PLACE_BESIDE_SOURCE,
  WORKSPACE_OPEN_ITEM,
  applyWorkspaceRequestsToFlatTabs,
  builtAppWorkspaceRequest,
  workspaceRequestFromSystemEvent,
  workspaceRequestsForBuiltApps,
} from '../workspacePlacement.js'

test('built app placement names intent without naming a tab strip or pane', () => {
  assert.deepEqual(builtAppWorkspaceRequest('chat-a', 42), {
    type: WORKSPACE_OPEN_ITEM,
    item: makeTab('app', 42),
    source: makeTab('chat', 'chat-a'),
    placement: PLACE_BESIDE_SOURCE,
    activation: ACTIVATE_IN_BACKGROUND,
    reason: 'chat-built-app',
  })
})

test('app_created is the only live event that requests first placement', () => {
  assert.deepEqual(
    workspaceRequestFromSystemEvent({
      type: 'app_created', appId: '42', chatId: 'chat-a',
    }),
    builtAppWorkspaceRequest('chat-a', 42),
  )
  assert.equal(workspaceRequestFromSystemEvent({
    type: 'app_updated', appId: '42', chatId: 'chat-a',
  }), null)
  assert.equal(workspaceRequestFromSystemEvent({
    type: 'app_created', appId: '42',
  }), null)
})

test('invalid app and chat identities cannot produce workspace requests', () => {
  assert.equal(builtAppWorkspaceRequest('', 42), null)
  assert.equal(builtAppWorkspaceRequest(null, 42), null)
  assert.equal(builtAppWorkspaceRequest('chat-a', 0), null)
  assert.equal(builtAppWorkspaceRequest('chat-a', 'not-an-id'), null)
})

test('flat projection keeps simultaneous chat builds beside their owners', () => {
  const apps = [
    { id: 41, chat_id: 'chat-a' },
    { id: 51, chat_id: 'chat-b' },
    { id: 42, chat_id: 'chat-a' },
  ]
  const arrivals = freshChatBuiltApps(apps, [41, 51, 42])
  const requests = workspaceRequestsForBuiltApps(arrivals)
  const after = applyWorkspaceRequestsToFlatTabs([
    makeTab('chat', 'chat-a'),
    makeTab('chat', 'chat-b'),
  ], requests)

  assert.deepEqual(after, [
    makeTab('chat', 'chat-a'),
    makeTab('app', 41),
    makeTab('app', 42),
    makeTab('chat', 'chat-b'),
    makeTab('app', 51),
  ])
})

test('replayed placement is a strict no-op and never changes focus', () => {
  const tabs = [makeTab('chat', 'chat-a'), makeTab('app', 42)]
  const request = builtAppWorkspaceRequest('chat-a', 42)

  assert.equal(applyWorkspaceRequestsToFlatTabs(tabs, [request]), tabs)
  assert.equal(request.activation, ACTIVATE_IN_BACKGROUND)
})

test('flat resolver ignores unsupported future requests', () => {
  const tabs = [makeTab('chat', 'chat-a')]
  const unsupported = {
    ...builtAppWorkspaceRequest('chat-a', 42),
    placement: 'replace-source',
  }
  assert.equal(applyWorkspaceRequestsToFlatTabs(tabs, [unsupported]), tabs)
})
