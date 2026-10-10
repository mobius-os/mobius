import test from 'node:test'
import assert from 'node:assert/strict'
import { createChatRowRefresh } from '../chatRowRefresh.js'

function harness({ fullReadInFlight = () => false } = {}) {
  const timers = []
  const reads = []
  const applied = []
  let refreshedAll = 0
  let started = 0
  let landed = 0
  const refresh = createChatRowRefresh({
    readRows: ids => new Promise((resolve, reject) => {
      reads.push({ ids, resolve, reject })
    }),
    applyRows: (ids, rows) => applied.push({ ids, rows }),
    refreshAll: async () => { refreshedAll += 1 },
    fullReadInFlight,
    fullReadMark: () => started,
    fullReadLandedSince: mark => landed > mark,
    schedule: fn => { timers.push(fn); return timers.length },
    unschedule: () => {},
  })
  const settle = () => new Promise(resolve => setTimeout(resolve, 0))
  return {
    refresh, timers, reads, applied, settle,
    fireTimer: () => timers.shift()(),
    startFullRead: () => ++started,
    landFullRead: read => { landed = Math.max(landed, read) },
    get refreshedAll() { return refreshedAll },
  }
}

test('a burst of run events becomes one scoped read of those rows', async () => {
  const h = harness()
  h.refresh.request('a')
  h.refresh.request('b')
  h.refresh.request('a')
  assert.equal(h.timers.length, 1)
  h.fireTimer()
  assert.deepEqual(h.reads.map(read => read.ids), [['a', 'b']])
  h.reads[0].resolve([{ id: 'a' }])
  await h.settle()
  assert.deepEqual(h.applied, [{ ids: ['a', 'b'], rows: [{ id: 'a' }] }])
  assert.equal(h.refreshedAll, 0)
})

test('events during a scoped read wait for exactly one follow-up read', async () => {
  const h = harness()
  h.refresh.request('a')
  h.fireTimer()
  h.refresh.request('a')
  h.refresh.request('c')
  assert.equal(h.timers.length, 0, 'no second read may overlap the first')
  h.reads[0].resolve([{ id: 'a', running: true }])
  await h.settle()
  assert.equal(h.timers.length, 1)
  h.fireTimer()
  assert.deepEqual(h.reads.map(read => read.ids), [['a'], ['a', 'c']])
})

test('a complete read in flight defers the batch until it lands, never starting another', async () => {
  let inFlight = true
  const h = harness({ fullReadInFlight: () => inFlight })
  h.refresh.request('a')
  h.fireTimer()
  await h.settle()
  assert.equal(h.reads.length, 0)
  assert.equal(h.refreshedAll, 0, 'replacing it would pile up server list builds')
  assert.equal(h.timers.length, 1, 'the batch retries after the next interval')
  h.refresh.request('b')
  inFlight = false
  h.fireTimer()
  assert.deepEqual(h.reads.map(read => read.ids), [['a', 'b']])
})

test('a complete read that starts and lands during the scoped read supersedes it', async () => {
  const h = harness()
  h.refresh.request('a')
  h.fireTimer()
  h.landFullRead(h.startFullRead())
  h.reads[0].resolve([{ id: 'a', running: true }])
  await h.settle()
  assert.deepEqual(h.applied, [])
  assert.equal(h.refreshedAll, 0)
})

test('a complete read that has not landed does not suppress the scoped answer', async () => {
  // It may fail or be cancelled (chat creation cancels list reads); if it
  // lands later it is newer and overwrites these rows anyway.
  const h = harness()
  h.refresh.request('server-created')
  h.fireTimer()
  h.startFullRead()
  h.reads[0].resolve([{ id: 'server-created' }])
  await h.settle()
  assert.deepEqual(h.applied, [{ ids: ['server-created'], rows: [{ id: 'server-created' }] }])
})

test('cancel is final even while a scoped read is in flight', async () => {
  const h = harness()
  h.refresh.request('a')
  h.fireTimer()
  h.refresh.request('b')
  h.refresh.cancel()
  h.reads[0].resolve([{ id: 'a' }])
  await h.settle()
  assert.deepEqual(h.applied, [])
  assert.equal(h.timers.length, 0)
})

test('a failed scoped read, such as one over the id bound, becomes a complete read', async () => {
  const h = harness()
  h.refresh.request('a')
  h.fireTimer()
  h.reads[0].reject(new Error('422'))
  await h.settle()
  assert.deepEqual(h.applied, [])
  assert.equal(h.refreshedAll, 1)
})
