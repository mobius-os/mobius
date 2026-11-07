/* Picker-visibility saves: a failed save never leaves the switches disagreeing with the server, and neither later toggles nor a refresh mid-queue overwrite earlier ones. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createLatestSaveQueue } from '../modelPrefsSaveQueue.js'

function harness(responses) {
  let shown = []
  let server = []
  const sent = []
  const pending = []
  const queue = createLatestSaveQueue({
    read: () => [...shown],
    show: list => { shown = [...list] },
    save: list => new Promise(resolve => pending.push({ list: [...list], resolve })),
  })
  const settleNext = async () => {
    const call = pending.shift()
    sent.push(call.list)
    const ok = responses.shift()
    if (ok) server = call.list
    call.resolve(ok)
    await new Promise(r => setImmediate(r))
  }
  const hide = id => queue.change(list => [...new Set([...list, id])])
  // A background refresh replacing the shown list with what the server held when it was read.
  const refreshShows = list => { shown = [...list] }
  return { hide, settleNext, refreshShows, state: () => ({ shown, server, sent }) }
}

test('a failed first save followed by a successful queued save leaves the switches matching the server', async () => {
  const h = harness([false, true, true])
  const first = h.hide('A')
  h.hide('B') // queued while the first save is in flight
  await h.settleNext() // [A] fails
  await h.settleNext() // [A, B] succeeds
  await first
  assert.deepEqual(h.state().shown, ['A', 'B'])
  assert.deepEqual(h.state().server, ['A', 'B'])
  // The next toggle builds on the confirmed list instead of a stale one.
  const third = h.hide('C')
  await h.settleNext()
  await third
  assert.deepEqual(h.state().server, ['A', 'B', 'C'])
  assert.deepEqual(h.state().shown, ['A', 'B', 'C'])
})

test('a failed save with nothing newer pending restores what the server holds', async () => {
  const h = harness([true, false])
  const first = h.hide('A')
  await h.settleNext()
  await first
  const second = h.hide('B')
  await h.settleNext()
  await second
  assert.deepEqual(h.state().server, ['A'])
  assert.deepEqual(h.state().shown, ['A'])
})

test('saves never overlap and the last one always carries every pending toggle', async () => {
  const h = harness([true, true])
  const first = h.hide('A')
  h.hide('B')
  h.hide('C')
  await h.settleNext()
  await h.settleNext()
  await first
  assert.deepEqual(h.state().sent, [['A'], ['A', 'B', 'C']])
  assert.deepEqual(h.state().server, ['A', 'B', 'C'])
})

test('a refresh landing mid-queue cannot make the next save drop or revert a toggle', async () => {
  const h = harness([true, true])
  const first = h.hide('A')
  h.hide('B')
  h.refreshShows([]) // a read that started before either toggle lands now
  await h.settleNext()
  await h.settleNext()
  await first
  assert.deepEqual(h.state().sent, [['A'], ['A', 'B']])
  assert.deepEqual(h.state().server, ['A', 'B'])
  assert.deepEqual(h.state().shown, ['A', 'B'])
})
