// History batches preserve contiguous offsets and indivisible display groups.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { olderHistoryBatches } from '../olderHistoryBatches.js'

const user = id => ({ role: 'user', id })
const assistant = id => ({ role: 'assistant', id })

test('older page paints newest first in bounded contiguous batches with exact offsets', () => {
  const older = Array.from({ length: 20 }, (_, i) => user(`u${i}`))
  const visible = [user('current')]
  const batches = olderHistoryBatches(older, visible, 40)
  assert.deepEqual(batches.map(batch => batch.rows.length), [5, 5, 5, 5])
  let painted = visible
  for (const batch of batches) {
    painted = [...batch.rows, ...painted]
    assert.equal(batch.offset + painted.length, 61)
  }
  assert.deepEqual(painted, [...older, ...visible])
})

test('batch cuts do not bisect a hidden-carrier assistant reply', () => {
  const older = [user('before'), assistant('run:assistant:1'),
    { role: 'user', hidden: true, steered: true, id: 'steer' },
    assistant('run:assistant:2'), user('after'), user('newest')]
  const batches = olderHistoryBatches(older, [user('current')], 10, 2)
  assert.ok(batches.some(batch => batch.rows.length === 3
    && batch.rows[0] === older[1] && batch.rows[2] === older[3]))
  assert.deepEqual(batches.flatMap(batch => batch.rows).map(row => row.id),
    ['after', 'newest', 'run:assistant:1', 'steer', 'run:assistant:2', 'before'])
})

test('provider owner batches stay together across a page batch boundary', () => {
  const rows = Array.from({ length: 3 }, (_, index) => ({
    ...user(`batched-${index}`), provider_batch: { id: 'batch', index, count: 3 },
  }))
  const older = [user('before'), ...rows, user('after'), user('newest')]
  const batches = olderHistoryBatches(older, [], 0, 2)
  assert.ok(batches.some(batch => batch.rows.length === 3
    && batch.rows[0] === rows[0] && batch.rows[2] === rows[2]))
})

test('an assistant reply crossing the fetched-page seam is installed together', () => {
  const older = [user('before'), assistant('run:assistant:1'),
    { role: 'user', hidden: true, steered: true, id: 'steer' }]
  const visible = [assistant('run:assistant:2'), user('tail')]
  const batches = olderHistoryBatches(older, visible, 7, 1)
  assert.deepEqual(batches.map(batch => batch.rows.length), [2, 1])
  assert.deepEqual(batches[0].rows, older.slice(1))
})

test('a blocked target cut chooses the smaller safe batch before growing it', () => {
  const group = Array.from({ length: 3 }, (_, index) => ({
    ...user(`batch-${index}`), provider_batch: { id: 'batch', index, count: 3 },
  }))
  const older = [user('u0'), user('u1'), ...group, user('u5'), user('u6'), user('u7')]
  const batches = olderHistoryBatches(older, [], 0, 5)
  assert.deepEqual(batches.map(batch => batch.rows.length), [3, 5],
    'the first five-row target lands inside the group; cut after it at index five')
  assert.deepEqual(batches[0].rows, older.slice(5))
})
