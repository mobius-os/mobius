/**
 * Behaviour of the per-message order between queued edits and the other
 * actions on that message (cancel, Send now), driven with deferred saves and
 * the real usePendingQueue. ChatView's handlers run EVERY operation on one
 * message through `run`: handleUpdatePending each save, handleCancelPending
 * the cancellation, handleSteerOne the send — so terminal actions wait out
 * earlier edits, and later edits wait for a terminal action's outcome.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import usePendingQueue from '../usePendingQueue.js'
import { createQueuedEditOrder } from '../../queuedEditOrder.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const turn = () => new Promise(resolve => setTimeout(resolve, 0))

const ROW = { role: 'user', content: 'Then check the dark theme', ts: 100, cid: 'q1', queued: true }
const EDITED = 'Then check the dark theme and the README'

/** A queue holding one server-confirmed row, and an edit of it that — like
 * ChatView's save — hydrates the queue from the server's answer. */
function queueWithEditInFlight(order) {
  const { result } = renderHook(usePendingQueue)
  result.current.hydrate([ROW], { revision: 0 })
  const response = deferred()
  const edit = order.run('q1', async () => {
    result.current.hydrate(await response.promise, { revision: 0 })
    return 'saved'
  })
  return { queue: result, edit, respond: rows => response.resolve(rows) }
}

const cids = queue => queue.current.pendingMessagesRef.current.map(row => row.cid)

test('cancelling a queued message waits for its in-flight edit', async () => {
  const order = createQueuedEditOrder()
  const save = deferred()
  const log = []
  const edit = order.run('q1', () => save.promise.then(outcome => { log.push('edit'); return outcome }))
  const cancel = order.run('q1', () => log.push('cancel'))

  await turn()
  assert.deepEqual(log, [], 'the cancel holds while the edit is saving')
  save.resolve('saved')
  assert.equal(await edit, 'saved', 'the edit still reports its own outcome')
  await cancel
  assert.deepEqual(log, ['edit', 'cancel'])
})

test('a late edit response cannot put a cancelled message back in the tray', async () => {
  const order = createQueuedEditOrder()
  const { queue, edit, respond } = queueWithEditInFlight(order)
  const cancel = order.run('q1', () => queue.current.cancelByCid('q1'))

  respond([{ ...ROW, content: EDITED }])
  await edit
  await cancel
  assert.deepEqual(cids(queue), [], 'the cancel lands after the edit response')

  // The failure mode the order prevents: cancel first, then the same late
  // response hydrates the row straight back.
  const { result: unordered } = renderHook(usePendingQueue)
  unordered.current.hydrate([ROW], { revision: 0 })
  unordered.current.cancelByCid('q1')
  unordered.current.hydrate([{ ...ROW, content: EDITED }], { revision: 0 })
  assert.deepEqual(cids(unordered), ['q1'])
})

test('Send now sends the saved edit, not the text it replaces', async () => {
  const order = createQueuedEditOrder()
  const { queue, edit, respond } = queueWithEditInFlight(order)
  const sentText = order.run('q1', () => (
    queue.current.pendingMessagesRef.current.find(row => row.cid === 'q1').content
  ))

  respond([{ ...ROW, content: EDITED }])
  await edit
  assert.equal(await sentText, EDITED)
})

test('an edit opened during a slow cancellation waits for its outcome', async () => {
  // The reopened pencil's save must not race the DELETE: it runs only after
  // the cancellation settles, when the queue already reflects the outcome.
  const order = createQueuedEditOrder()
  const { result: queue } = renderHook(usePendingQueue)
  queue.current.hydrate([ROW], { revision: 0 })
  const confirm = deferred()
  const log = []
  const cancel = order.run('q1', async () => {
    await confirm.promise
    queue.current.cancelByCid('q1')
    log.push('cancelled')
  })
  const lateEdit = order.run('q1', () => {
    log.push(`edit sees ${cids(queue).length ? 'the row' : 'no row'}`)
    return cids(queue).length ? 'saved' : 'gone'
  })

  await turn()
  assert.deepEqual(log, [], 'the edit holds while the cancellation runs')
  confirm.resolve()
  await cancel
  assert.equal(await lateEdit, 'gone', 'the deferred edit saves against the settled outcome')
  assert.deepEqual(log, ['cancelled', 'edit sees no row'])
})

test('edits of one message save one after another', async () => {
  const order = createQueuedEditOrder()
  const first = deferred()
  const second = deferred()
  const started = []
  const firstEdit = order.run('q1', () => { started.push('first'); return first.promise })
  const secondEdit = order.run('q1', () => { started.push('second'); return second.promise })
  let cancelled = false
  const cancel = order.run('q1', () => { cancelled = true })

  await turn()
  assert.deepEqual(started, ['first'], 'a newer edit cannot overtake an older one')
  first.resolve('saved')
  await firstEdit
  await turn()
  assert.deepEqual(started, ['first', 'second'])
  assert.equal(cancelled, false, 'a cancel waits for every edit started before it')
  second.resolve('saved')
  await secondEdit
  await cancel
  assert.equal(cancelled, true)
})

test('a failed edit still releases its message, and other messages never wait', async () => {
  const order = createQueuedEditOrder()
  const save = deferred()
  const edit = order.run('q1', () => save.promise)

  let otherReady = false
  void order.run('q2', () => { otherReady = true })
  await turn()
  assert.equal(otherReady, true, 'an edit of q1 never holds q2')

  save.reject(new Error('offline'))
  await assert.rejects(edit, /offline/, 'the edit reports its own failure')
  await turn()
  let ready = false
  void order.run('q1', () => { ready = true })
  await turn()
  assert.equal(ready, true, 'nothing is left waiting once the failed edit settles')
})

test('Steer all and Stop read the queue only after every row’s edit has saved', async () => {
  const order = createQueuedEditOrder()
  const { queue, respond } = queueWithEditInFlight(order)
  const otherSave = deferred()
  order.run('q2', () => otherSave.promise)
  let settled = false
  const wholeQueue = order.settled().then(() => { settled = true })

  await turn()
  assert.equal(settled, false, 'an edit in flight holds the whole-queue action')
  respond([{ ...ROW, content: EDITED }])
  await turn()
  assert.equal(settled, false, 'every row’s chain must settle, not just one')
  otherSave.reject(new Error('save failed'))
  await wholeQueue
  assert.equal(queue.current.pendingMessagesRef.current[0].content, EDITED,
    'the snapshot sees the saved text, not the text the edit replaced')
})

test('a whole-queue action never waits when nothing is saving', async () => {
  const order = createQueuedEditOrder()
  await order.run('q1', async () => 'saved')
  let settled = false
  order.settled().then(() => { settled = true })
  await turn()
  assert.equal(settled, true)
})
