import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useTranscriptState from '../useTranscriptState.js'

function queryClientWith(initial) {
  let value = initial
  let writes = 0
  return {
    get value() { return value },
    get writes() { return writes },
    setQueryData(_key, updater) {
      writes += 1
      value = updater(value)
    },
  }
}

test('consecutive transcript commits compose through the synchronous owner ref', () => {
  const queryClient = queryClientWith({ messages: [], offset: 0, updated_at: 'proof' })
  const { result } = renderHook(useTranscriptState, {
    cacheKey: ['chat-messages', 7],
    cached: queryClient.value,
    queryClient,
  })

  result.current.commitMessages(prev => [...prev, { ts: 1 }])
  result.current.commitMessages(prev => [...prev, { ts: 2 }])

  assert.deepEqual(result.current.messages, [{ ts: 1 }, { ts: 2 }])
  assert.deepEqual(result.current.messagesRef.current, [{ ts: 1 }, { ts: 2 }])
  assert.equal(queryClient.writes, 2)
  assert.equal(queryClient.value.updated_at, null)
})

test('an authoritative view activation does not republish the query cache', () => {
  const queryClient = queryClientWith({ messages: [{ ts: 1 }], offset: 0 })
  const { result } = renderHook(useTranscriptState, {
    cacheKey: ['chat-messages', 7],
    cached: queryClient.value,
    queryClient,
  })

  result.current.applyMessagesToView([{ ts: 1 }, { ts: 2 }], 12)

  assert.deepEqual(result.current.messages, [{ ts: 1 }, { ts: 2 }])
  assert.equal(result.current.offset, 12)
  assert.equal(queryClient.writes, 0)
})

test('structurally identical commits still publish but avoid replacing view state', () => {
  const first = [{ role: 'user', content: 'same', ts: 1 }]
  const queryClient = queryClientWith({ messages: first, offset: 0 })
  const { result } = renderHook(useTranscriptState, {
    cacheKey: ['chat-messages', 7],
    cached: queryClient.value,
    queryClient,
  })

  result.current.commitMessages([{ ...first[0] }])

  assert.equal(result.current.messages, first)
  assert.equal(queryClient.writes, 1)
  assert.notEqual(queryClient.value.messages, first)
})
