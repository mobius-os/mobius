/* Resume owns a lifecycle transaction, never draft or queued follow-up state. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useResume from '../useResume.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fixture(send, changes = {}) {
  const accepted = [], refreshes = []
  const props = {
    chatId: 'chat-a', runId: 'interrupted-a', send,
    onAccepted: value => accepted.push(value),
    onRefresh: () => refreshes.push(true),
    blocked: () => false,
    ...changes,
  }
  const hook = renderHook(() => useResume(props))
  return { ...hook, props, accepted, refreshes }
}

test('Continue anyway binds explicit storage permission to the same acknowledged retry', async () => {
  const requests = []
  let failed = true
  const h = fixture(async (...args) => {
    requests.push(args)
    if (failed) throw Object.assign(new Error('network'), { outboxRetained: true })
    return { status: 'started' }
  })
  await h.result.current.resume({ allowLowStorage: true })
  h.rerender()
  failed = false
  await h.result.current.resume({ allowLowStorage: true })
  assert.deepEqual(requests[1], requests[0])
  assert.equal(requests[0][2].allowLowStorage, true)
  assert.equal(requests[0][2].resumeRunId, 'interrupted-a')
  assert.equal(requests[0][0], '', 'the action is not owner-authored prose')
})

test('a saved retry is not reused when the other recovery action is pressed', async () => {
  const requests = []
  const h = fixture(async (...args) => {
    requests.push(args)
    throw Object.assign(new Error('network'), { outboxRetained: true })
  })
  await h.result.current.resume()
  h.rerender()
  await h.result.current.resume({ allowLowStorage: true })
  assert.match(h.result.current.state.error, /Continue anyway is saved/)
  h.rerender()
  await h.result.current.resume()
  assert.equal(requests[0][2].allowLowStorage, undefined)
  assert.equal(requests[1][2].allowLowStorage, true)
  assert.notEqual(requests[1][2].cid, requests[0][2].cid)
  assert.equal(requests[2][2].allowLowStorage, undefined)
  assert.notEqual(requests[2][2].cid, requests[1][2].cid)
})

test('Resume stays pending without emitting a row until server acceptance; repeated taps coalesce', async () => {
  const response = deferred(), requests = []
  const h = fixture((...args) => { requests.push(args); return response.promise })
  const first = h.result.current.resume()
  assert.equal(h.result.current.state.pending, true)
  assert.deepEqual(h.accepted, [])
  assert.equal(await h.result.current.resume(), false)
  assert.equal(requests.length, 1)
  assert.equal(requests[0][0], '')
  assert.equal(requests[0][1], undefined, 'no composer attachments can enter the control')
  assert.deepEqual(Object.keys(requests[0][2]).sort(), ['cid', 'continuation', 'resumeRunId'])
  assert.equal(requests[0][2].resumeRunId, 'interrupted-a')
  const result = { status: 'started' }
  response.resolve(result)
  assert.equal(await first, true)
  assert.deepEqual(h.accepted, [result])
  assert.equal(h.result.current.state.pending, false)
  assert.equal(h.refreshes.length, 1)
})

test('ambiguous Resume retry keeps exact request identity and interrupted target', async () => {
  const requests = []
  let failed = true
  const h = fixture(async (...args) => {
    requests.push(args)
    if (failed) throw Object.assign(new Error('network'), { outboxRetained: true })
    return { status: 'duplicate', running: true }
  })
  assert.equal(await h.result.current.resume(), false)
  assert.match(h.result.current.state.error, /Resume is saved/)
  assert.deepEqual(h.accepted, [])
  h.rerender()
  failed = false
  assert.equal(await h.result.current.resume(), true)
  assert.deepEqual(requests[1], requests[0], 'replay cannot target a newer turn')
})

test('authoritative rejection leaves the recovery action retryable after refreshed details', async () => {
  let reject = true
  const requests = []
  const h = fixture(async (...args) => {
    requests.push(args)
    if (reject) throw Object.assign(new Error('stale'), { status: 409 })
    return { status: 'started' }
  })
  await h.result.current.resume()
  assert.equal(h.result.current.state.pending, false)
  assert.match(h.result.current.state.error, /draft and queued messages are unchanged/)
  h.props.runId = 'new-interrupted-run'
  h.rerender()
  reject = false
  await h.result.current.resume()
  assert.notEqual(requests[1][2].cid, requests[0][2].cid)
  assert.equal(requests[1][2].resumeRunId, 'new-interrupted-run')
})

test('switching chats fences a late Resume acknowledgement', async () => {
  const response = deferred()
  const h = fixture(() => response.promise)
  const pending = h.result.current.resume()
  h.props.chatId = 'chat-b'
  h.props.runId = 'b'
  h.rerender()
  response.resolve({ status: 'started' })
  assert.equal(await pending, false)
  assert.deepEqual(h.accepted, [])
  assert.deepEqual(h.refreshes, [])
  assert.equal(h.result.current.state.pending, false)
})

test('missing recovery identity never sends an untargeted continuation', async () => {
  let sent = false
  const h = fixture(() => { sent = true }, { runId: null })
  assert.equal(await h.result.current.resume(), false)
  assert.equal(sent, false)
  assert.equal(h.refreshes.length, 1)
})

test('provider switch or already running turn prevents Resume without changing state', async () => {
  let sent = false
  const h = fixture(() => { sent = true }, { blocked: () => true })
  assert.equal(await h.result.current.resume(), false)
  assert.equal(sent, false)
  assert.deepEqual(h.result.current.state, {
    pending: false, error: '', unavailable: false,
  })
})


test('a newer recovery target retires the previous ambiguous action instead of retargeting it', async () => {
  const requests = []
  let failure = true
  const h = fixture(async (...args) => {
    requests.push(args)
    if (failure) throw Object.assign(new Error('network'), { outboxRetained: true })
    return { status: 'started' }
  })
  await h.result.current.resume()
  h.props.runId = 'new-interrupted-b'
  h.rerender()
  failure = false
  await h.result.current.resume()
  assert.notEqual(requests[1][2].cid, requests[0][2].cid)
  assert.equal(requests[1][2].resumeRunId, 'new-interrupted-b')
  assert.equal(requests[0][2].resumeRunId, 'interrupted-a')
})

for (const outcome of ['accepted', 'network-error']) {
  test(`runtime advance during ${outcome} still refreshes this chat without applying an obsolete Resume result`, async () => {
    const response = deferred()
    const h = fixture(() => response.promise)
    const pending = h.result.current.resume()
    // Runtime can observe the successor before the POST acknowledgement (or
    // its idempotent retry) arrives. The exact Resume action is now obsolete,
    // but its durable transcript still needs reconciling in the same chat.
    h.props.runId = null
    h.rerender()
    if (outcome === 'accepted') response.resolve({ status: 'duplicate', running: true })
    else response.reject(new Error('lost acknowledgement'))
    assert.equal(await pending, false)
    assert.deepEqual(h.accepted, [])
    assert.equal(h.refreshes.length, 1)
    assert.equal(h.result.current.state.error, '')
    h.unmount()
  })
}

test('Goal Resume survives an unrelated ended turn without a physical recovery id', async () => {
  const requests = []
  const h = fixture(async (...args) => { requests.push(args); return { status: 'started' } }, {
    runId: null, goalId: 'held-goal', goalRevision: 7,
  })
  assert.equal(h.result.current.state.unavailable, false)
  assert.equal(await h.result.current.resume(), true)
  assert.deepEqual(requests[0][2], {
    cid: requests[0][2].cid, continuation: 'manual', resumeGoalId: 'held-goal', resumeGoalRevision: 7,
  })
})

test('ambiguous Goal Resume retries retain identity but a new revision cannot retarget the old attempt', async () => {
  const requests = []
  const h = fixture(async (...args) => {
    requests.push(args)
    throw Object.assign(new Error('network'), { outboxRetained: true })
  }, { runId: null, goalId: 'held-goal', goalRevision: 7 })
  await h.result.current.resume()
  h.rerender()
  await h.result.current.resume()
  assert.deepEqual(requests[1], requests[0])
  h.props.goalRevision = 8
  h.rerender()
  await h.result.current.resume()
  assert.notEqual(requests[2][2].cid, requests[0][2].cid)
  assert.equal(requests[2][2].resumeGoalRevision, 8)
  assert.equal(requests[0][2].resumeGoalRevision, 7)
})

test('a stale Goal revision refreshes instead of pretending continuation was accepted', async () => {
  const h = fixture(async () => {
    throw Object.assign(new Error('stale'), { status: 409, code: 'recovery_changed' })
  }, { runId: null, goalId: 'held-goal', goalRevision: 7 })
  assert.equal(await h.result.current.resume(), false)
  assert.deepEqual(h.accepted, [])
  assert.equal(h.refreshes.length, 1)
  assert.match(h.result.current.state.error, /Recovery state changed/)
})

for (const field of ['goalId', 'goalRevision']) {
  test(`changed ${field} fences a late Goal Resume acknowledgement`, async () => {
    const response = deferred()
    const h = fixture(() => response.promise, { runId: null, goalId: 'held-goal', goalRevision: 7 })
    const pending = h.result.current.resume()
    h.props[field] = field === 'goalId' ? 'replacement-goal' : 8
    h.rerender()
    response.resolve({ status: 'started' })
    assert.equal(await pending, false)
    assert.deepEqual(h.accepted, [])
    assert.equal(h.refreshes.length, 1)
  })
}

test('Goal Resume requires its revision and never falls back to an unrelated run', async () => {
  let sent = false
  const h = fixture(() => { sent = true }, { goalId: 'held-goal' })
  assert.equal(await h.result.current.resume(), false)
  assert.equal(sent, false)
  assert.equal(h.result.current.state.unavailable, true)
})

test('pending question and live-turn guards also block exact Goal Resume', async () => {
  let sent = false
  const h = fixture(() => { sent = true }, {
    runId: null, goalId: 'held-goal', goalRevision: 7, blocked: () => true,
  })
  assert.equal(await h.result.current.resume(), false)
  assert.equal(sent, false)
})
