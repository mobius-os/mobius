import { test } from 'node:test'
import assert from 'node:assert/strict'

import { makeEmbedAuthorizationHandoff } from '../../../public/mobius-runtime.js'

function fakeTimers() {
  let nextId = 0
  const timers = new Map()
  return {
    setTimer(callback, delay) {
      const id = ++nextId
      timers.set(id, { callback, delay })
      return id
    },
    clearTimer(id) { timers.delete(id) },
    runDelay(delay) {
      const match = [...timers].find(([, timer]) => timer.delay === delay)
      assert.ok(match, `expected an active ${delay}ms timer`)
      const [id, timer] = match
      timers.delete(id)
      timer.callback()
    },
    get size() { return timers.size },
  }
}

const flush = () => new Promise((resolve) => setImmediate(resolve))

function harness({ mint } = {}) {
  const timers = fakeTimers()
  const posts = []
  const errors = []
  let authSequence = 0
  let grantSequence = 0
  const handoff = makeEmbedAuthorizationHandoff({
    mint: mint || (async () => `grant-${++grantSequence}`),
    post: (message) => posts.push(message),
    onAttemptError: (error) => errors.push(error.message),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    makeAuthorizationId: () => `authorization-${++authSequence}`,
  })
  return { handoff, timers, posts, errors }
}

test('successful refresh hands off only after the correlated replacement is ready', async () => {
  const { handoff, posts, timers } = harness()
  handoff.start()
  await flush()
  assert.deepEqual(posts[0], {
    authorizationId: 'authorization-1',
    capability: 'grant-1',
  })
  assert.equal(handoff.ready('authorization-1'), true)
  assert.equal(timers.size, 0)

  handoff.refresh()
  await flush()
  assert.deepEqual(posts[1], {
    authorizationId: 'authorization-2',
    capability: 'grant-2',
  })
  assert.equal(handoff.ready('authorization-1'), false, 'late prior READY must not win')
  assert.equal(handoff.ready('authorization-2'), true)
  assert.equal(timers.size, 0)
})

test('transient mint failure recovers with bounded backoff and a fresh grant', async () => {
  let calls = 0
  const { handoff, posts, timers, errors } = harness({
    mint: async () => {
      calls += 1
      if (calls === 1) throw new Error('temporary network failure')
      return `grant-${calls}`
    },
  })
  handoff.start()
  await flush()
  assert.deepEqual(posts, [])
  assert.deepEqual(errors, ['temporary network failure'])
  timers.runDelay(250)
  await flush()
  assert.equal(calls, 2)
  assert.equal(posts[0].capability, 'grant-2')
  assert.equal(handoff.ready(posts[0].authorizationId), true)
})

test('ambiguous consumed-grant failure retries by minting a new one-use grant', async () => {
  const { handoff, posts, timers } = harness()
  handoff.start()
  await flush()
  const first = posts[0]
  assert.equal(
    handoff.failed(first.authorizationId, new Error('exchange response was lost')),
    true,
  )
  timers.runDelay(250)
  await flush()
  const second = posts[1]
  assert.notEqual(second.capability, first.capability)
  assert.notEqual(second.authorizationId, first.authorizationId)
  assert.equal(handoff.ready(second.authorizationId), true)
})

test('destroy cancels pending acknowledgement and scheduled retry work', async () => {
  const first = harness()
  first.handoff.start()
  await flush()
  assert.equal(first.timers.size, 1)
  first.handoff.destroy()
  assert.equal(first.timers.size, 0)

  const second = harness()
  second.handoff.start()
  await flush()
  second.handoff.failed(second.posts[0].authorizationId, new Error('retry me'))
  assert.equal(second.timers.size, 1)
  second.handoff.destroy()
  assert.equal(second.timers.size, 0)
  assert.deepEqual(second.posts.map((post) => post.capability), ['grant-1'])
})
