/* Exercise the scoped drawer projection used by companion visibility events. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { createChatRowRefresh } from '../chatRowRefresh.js'
import { withRefreshedChatRows } from '../chatListProjection.js'
import { invalidateShellListCache } from '../../../api/client.js'
import { SHELL_DATA_CACHE } from '../../../sw-cache-policy.js'

function harness(initialRows) {
  let rows = initialRows
  let serverRows = []
  const scheduled = []
  const reads = []
  const evicted = []
  const invalidations = []
  let completeReads = 0
  const cacheStorage = {
    async open(name) {
      assert.equal(name, SHELL_DATA_CACHE)
      return { async delete(url) { evicted.push(url); return true } }
    },
  }
  const refresh = createChatRowRefresh({
    async readRows(ids) {
      reads.push(ids)
      return serverRows
    },
    applyRows(ids, fresh) {
      rows = withRefreshedChatRows(rows, ids, fresh)
      invalidations.push(invalidateShellListCache('chats', {
        cacheStorage, origin: 'https://mobius.test',
      }))
    },
    async refreshAll() { completeReads += 1 },
    fullReadInFlight: () => false,
    fullReadMark: () => 0,
    fullReadLandedSince: () => false,
    schedule: callback => { scheduled.push(callback); return scheduled.length },
    unschedule: () => {},
  })
  return {
    refresh, reads, evicted,
    get rows() { return rows },
    get completeReads() { return completeReads },
    async land(fresh) {
      serverRows = fresh
      assert.equal(scheduled.length, 1, 'one coalesced exact-row read')
      await scheduled.shift()()
      await Promise.all(invalidations)
    },
  }
}

test('visibility promotion adds only the named row and evicts the offline copy', async () => {
  const unrelated = { id: 'other', title: 'Other', running: true }
  const promoted = { id: 'companion', title: 'Companion', running: false }
  const h = harness([unrelated])
  h.refresh.request('companion')
  h.refresh.request('companion')
  await h.land([promoted])
  assert.deepEqual(h.reads, [['companion']])
  assert.deepEqual(h.rows, [unrelated, promoted])
  assert.equal(h.rows[0], unrelated)
  assert.deepEqual(h.evicted, ['https://mobius.test/api/chats'])
  assert.equal(h.completeReads, 0)
  h.refresh.cancel()
})

test('hiding removes a requested missing row without changing unrelated state', async () => {
  const unrelated = { id: 'other', title: 'Other', pending_question_id: 'card' }
  const h = harness([{ id: 'companion', title: 'Companion' }, unrelated])
  h.refresh.request('companion')
  await h.land([])
  assert.deepEqual(h.rows, [unrelated])
  assert.equal(h.rows[0], unrelated)
  assert.deepEqual(h.evicted, ['https://mobius.test/api/chats'])
  assert.equal(h.completeReads, 0)
  h.refresh.cancel()
})

test('promotion reads the committed rename and a later hide cannot resurrect it', async () => {
  const unrelated = { id: 'other', title: 'Other' }
  const h = harness([unrelated])
  h.refresh.request('companion')
  await h.land([{ id: 'companion', title: 'Committed name' }])
  assert.equal(h.rows[1].title, 'Committed name')
  h.refresh.request('companion')
  await h.land([])
  assert.deepEqual(h.rows, [unrelated])
  assert.equal(h.rows[0], unrelated)
  assert.deepEqual(h.reads, [['companion'], ['companion']])
  assert.equal(h.evicted.length, 2)
  assert.equal(h.completeReads, 0)
  h.refresh.cancel()
})
