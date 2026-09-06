import test from 'node:test'
import assert from 'node:assert/strict'

import {
  changesAttentionCursor,
  hasUnseenChangesAttention,
  readSeenChangesAttention,
  writeSeenChangesAttention,
} from '../brainChangesAttention.js'

function storageStub() {
  const values = new Map()
  return {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
  }
}

test('actionable Changes expose an exact cursor while settled work stays quiet', () => {
  assert.equal(changesAttentionCursor({
    lifecycleAvailable: true,
    needsAction: true,
    workflowRevision: 'revision-a',
  }), 'revision-a||')
  assert.equal(changesAttentionCursor({
    lifecycleAvailable: true,
    needsAction: false,
    workState: null,
  }), '')
})

test('opening the Brain acknowledges only the current Changes revision', () => {
  const storage = storageStub()
  const cursor = 'revision-a||'
  assert.equal(hasUnseenChangesAttention(cursor, readSeenChangesAttention('chat-a', storage)), true)
  assert.equal(writeSeenChangesAttention('chat-a', cursor, storage), true)
  assert.equal(readSeenChangesAttention('chat-a', storage), cursor)
  assert.equal(hasUnseenChangesAttention(cursor, cursor), false)
  assert.equal(hasUnseenChangesAttention('revision-b||', cursor), true)
})

test('attention work contributes its durable run identity to the cursor', () => {
  assert.equal(changesAttentionCursor({
    lifecycleAvailable: true,
    needsAction: false,
    workflowRevision: '',
    workState: 'attention',
    work: { id: 'work-7', status: 'failed', updated_at: 't2' },
  }), '||work-7:failed:t2')
})
