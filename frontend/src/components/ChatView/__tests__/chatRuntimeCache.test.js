import { test } from 'node:test'
import assert from 'node:assert/strict'

import { updateChatRuntimeCache } from '../chatRuntimeCache.js'

function cacheHarness(initial) {
  let value = initial
  let updates = 0
  return {
    queryClient: {
      getQueryData() {
        return value
      },
      setQueryData(_key, updater) {
        updates += 1
        value = updater(value)
      },
    },
    value: () => value,
    updates: () => updates,
  }
}

test('unchanged runtime polls do not publish a persisted-cache update', () => {
  const transcript = [{ role: 'assistant', content: 'large durable history' }]
  const cache = cacheHarness({
    messages: transcript,
    running: true,
    activeGoalObjective: 'Fix first-scroll jitter',
    goal: {
      id: 'goal-1', objective: 'Fix first-scroll jitter', status: 'active',
    },
    pending_messages: [{ id: 'queued-1', content: 'next' }],
    pending_question_id: null,
  })

  updateChatRuntimeCache(
    cache.queryClient,
    ['chat-messages', 'chat-1'],
    {
      running: true,
      activeGoalObjective: 'Fix first-scroll jitter',
      goal: {
        id: 'goal-1', objective: 'Fix first-scroll jitter', status: 'active',
      },
      // A network response creates new objects even when their JSON-domain
      // value is unchanged. That must still count as an unchanged poll.
      pending_messages: [{ id: 'queued-1', content: 'next' }],
      pending_question_id: null,
    },
  )

  assert.equal(cache.updates(), 0, 'setQueryData itself must be skipped')
  assert.equal(cache.value().messages, transcript)
})

test('unchanged durable waits do not republish the persisted chat cache', () => {
  const waits = [{ id: 'wait-1', description: 'CI finishes', status: 'armed' }]
  const cache = cacheHarness({ messages: [], waits })

  updateChatRuntimeCache(
    cache.queryClient,
    ['chat-messages', 'chat-1'],
    { waits: [{ id: 'wait-1', description: 'CI finishes', status: 'armed' }] },
  )

  assert.equal(cache.updates(), 0)
  assert.equal(cache.value().waits, waits)
})

test('a runtime transition patches only runtime fields', () => {
  const transcript = [{ role: 'assistant', content: 'history' }]
  const cache = cacheHarness({
    messages: transcript,
    offset: 12,
    running: true,
    pending_messages: [{ id: 'queued-1' }],
    pending_question_id: null,
  })

  updateChatRuntimeCache(
    cache.queryClient,
    ['chat-messages', 'chat-1'],
    {
      running: false,
      pending_messages: [],
      pending_question_id: 'question-1',
    },
  )

  assert.equal(cache.updates(), 1)
  assert.equal(cache.value().messages, transcript)
  assert.equal(cache.value().offset, 12)
  assert.equal(cache.value().running, false)
  assert.deepEqual(cache.value().pending_messages, [])
  assert.equal(cache.value().pending_question_id, 'question-1')
})

test('a missing chat cache accepts the first runtime snapshot', () => {
  const cache = cacheHarness(undefined)

  updateChatRuntimeCache(
    cache.queryClient,
    ['chat-messages', 'chat-1'],
    { running: true },
  )
  assert.equal(cache.updates(), 1)
  assert.deepEqual(cache.value(), { running: true })
})
