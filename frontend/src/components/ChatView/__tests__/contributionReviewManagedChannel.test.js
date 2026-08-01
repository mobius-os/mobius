import test from 'node:test'
import assert from 'node:assert/strict'
import {
  sendBlocker,
  visibleReviewItems,
} from '../contributionReviewModel.js'


test('managed release records and stacks stay off the chat send surface', () => {
  const reason = 'Platform contributions are disabled on this release channel.'
  const record = {
    id: 'single', status: 'prepared', review: { state: 'ready' },
    contribution_disabled_reason: reason,
  }
  assert.equal(sendBlocker(record, { connected: true }), reason)
  assert.deepEqual(visibleReviewItems({ records: [record] }, null), [])

  const stack = [1, 2].map(position => ({
    id: `layer-${position}`,
    status: 'prepared',
    contribution_disabled_reason: reason,
    stack: { id: 'managed', position, total: 2 },
  }))
  assert.deepEqual(visibleReviewItems({ records: stack }, null), [])
})
