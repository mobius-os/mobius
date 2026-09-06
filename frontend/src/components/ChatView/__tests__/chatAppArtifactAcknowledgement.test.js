import test from 'node:test'
import assert from 'node:assert/strict'

import {
  acknowledgeChatArtifactRows,
  appTouchCursorsForBrainOpen,
  unseenAppTouchCursors,
} from '../chatAppArtifactAcknowledgement.js'

test('Brain opening captures only unseen app touch cursors', () => {
  const apps = [
    { id: 7, chat_touched_at: 'touch-a', has_unseen_chat_update: true },
    { id: 8, chat_touched_at: 'touch-b', has_unseen_chat_update: false },
    { id: 9, has_unseen_chat_update: true },
  ]
  assert.deepEqual(unseenAppTouchCursors(apps), [
    { app_id: 7, touched_at: 'touch-a' },
  ])
  assert.deepEqual(appTouchCursorsForBrainOpen(false, apps), [
    { app_id: 7, touched_at: 'touch-a' },
  ])
})

test('closing an open Brain does not acknowledge app updates', () => {
  assert.deepEqual(appTouchCursorsForBrainOpen(true, [
    { id: 7, chat_touched_at: 'touch-a', has_unseen_chat_update: true },
  ]), [])
})

test('optimistic acknowledgement cannot clear a newer concurrent touch', () => {
  const currentRows = [{
    app: { id: 7 },
    touched_at: 'touch-new',
    seen_at: 'touch-old',
  }]
  assert.equal(
    acknowledgeChatArtifactRows(
      currentRows,
      [{ app_id: 7, touched_at: 'touch-old' }],
    ),
    currentRows,
  )

  assert.deepEqual(acknowledgeChatArtifactRows(
    currentRows,
    [{ app_id: 7, touched_at: 'touch-new' }],
  ), [{
    app: { id: 7 },
    touched_at: 'touch-new',
    seen_at: 'touch-new',
  }])
})
