import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  appArtifactAttentionDecision,
  unseenAppArtifactCount,
} from '../appArtifactAttention.js'

const app = (id, touchedAt, unseen = true) => ({
  id,
  name: `App ${id}`,
  chat_touched_at: touchedAt,
  has_unseen_chat_update: unseen,
})

test('opening a chat queues every unread app newest first', () => {
  const decision = appArtifactAttentionDecision([
    app(7, '2026-08-29T10:00:00Z'),
    app(9, '2026-08-29T11:00:00Z'),
  ], null)
  assert.deepEqual(decision.dropApps.map(item => item.id), [9, 7])
  assert.equal(decision.nextTouches.get(7), '2026-08-29T10:00:00Z')
})

test('a new live touch still replays during the same visible chat lifetime', () => {
  const decision = appArtifactAttentionDecision(
    [app(7, 'touch-new')],
    new Map([[7, 'touch-old']]),
  )
  assert.equal(decision.dropApps[0].id, 7)
})

test('an unchanged or Brain-acknowledged touch never replays the icon drop', () => {
  const same = appArtifactAttentionDecision(
    [app(7, 't1')],
    new Map([[7, 't1']]),
  )
  assert.deepEqual(same.dropApps, [])
  const opened = appArtifactAttentionDecision(
    [app(7, 't2', false)],
    new Map([[7, 't1']]),
  )
  assert.deepEqual(opened.dropApps, [])
})

test('unseen count ignores ordinary app artifact rows', () => {
  assert.equal(unseenAppArtifactCount([app(7, 't1'), app(9, 't2', false)]), 1)
})
