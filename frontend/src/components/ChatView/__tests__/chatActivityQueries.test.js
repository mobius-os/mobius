/* Activity route and invalidation behavior stay exact-chat and read-only. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../../../api/client.js'
import {
  CHAT_ACTIVITY_STALE_TIME,
  chatActivityQueryKey,
  invalidateAllChatActivity,
  invalidateChatActivityForSystemEvent,
  retryChatActivity,
} from '../chatActivityQueries.js'
import { QueryClient } from '@tanstack/react-query'

function queryClientSpy() {
  const calls = []
  return {
    calls,
    invalidateQueries(options) {
      calls.push(options)
      return Promise.resolve()
    },
  }
}

test('activity client requests the encoded exact-chat page and forwards cancellation', async (t) => {
  const originalFetch = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return Response.json({ events: [], next_before: null })
  }
  t.after(() => { globalThis.fetch = originalFetch })
  const controller = new AbortController()

  const response = await api.chats.activity('parent/one', {
    before: 'cursor+/=', limit: 75, signal: controller.signal,
  })

  assert.deepEqual(await response.json(), { events: [], next_before: null })
  assert.equal(
    requests[0].url,
    '/api/chats/parent%2Fone/activity?limit=75&before=cursor%2B%2F%3D',
  )
  assert.equal(requests[0].options.signal, controller.signal)
  assert.equal(requests[0].options.method, undefined)
})

test('helper completion invalidates only its exact parent activity', async () => {
  const queryClient = queryClientSpy()
  await invalidateChatActivityForSystemEvent(queryClient, {
    type: 'chat_activity_changed', chatId: 'parent',
  })
  assert.deepEqual(queryClient.calls, [{
    queryKey: chatActivityQueryKey('parent'),
  }])
})

test('peer hints preserve directed and broadcast visibility', async () => {
  const queryClient = queryClientSpy()
  await invalidateChatActivityForSystemEvent(queryClient, {
    type: 'agent_coordination_message', senderChatId: 'sender',
    recipientChatIds: ['recipient'], broadcast: false,
  })
  const directed = queryClient.calls[0].predicate
  assert.equal(directed({ queryKey: chatActivityQueryKey('sender') }), true)
  assert.equal(directed({ queryKey: chatActivityQueryKey('recipient') }), true)
  assert.equal(directed({ queryKey: chatActivityQueryKey('unrelated') }), false)
  assert.equal(directed({ queryKey: ['chat-network-summary', 'sender'] }), false)

  const broadcastClient = queryClientSpy()
  await invalidateChatActivityForSystemEvent(broadcastClient, {
    type: 'agent_coordination_message', senderChatId: 'sender',
    recipientChatIds: [], broadcast: true,
  })
  assert.equal(
    broadcastClient.calls[0].predicate({ queryKey: chatActivityQueryKey('viewer') }),
    true,
  )
})

test('system reconnect refreshes active activity queries without a poll loop', async () => {
  const queryClient = queryClientSpy()
  await invalidateAllChatActivity(queryClient)
  assert.deepEqual(queryClient.calls, [{ queryKey: ['chat-activity'] }])
})

test('activity remains cached until an owning event invalidates it', () => {
  assert.equal(CHAT_ACTIVITY_STALE_TIME, Infinity)
})

test('activity retries restart-shaped failures but accepts a real 4xx answer', () => {
  const status = code => Object.assign(new Error('failed'), { status: code })
  assert.equal(retryChatActivity(0, new TypeError('Failed to fetch')), true)
  assert.equal(retryChatActivity(0, Object.assign(new Error('t'), { name: 'TimeoutError' })), true)
  assert.equal(retryChatActivity(0, status(502)), true)
  assert.equal(retryChatActivity(0, status(429)), true)
  assert.equal(retryChatActivity(3, status(503)), true)
  assert.equal(retryChatActivity(4, status(503)), false)
  assert.equal(retryChatActivity(0, status(404)), false)
  assert.equal(retryChatActivity(0, status(422)), false)
  assert.equal(retryChatActivity(0, Object.assign(new Error('a'), { name: 'AbortError' })), false)
})

test('a restart gap heals the activity query without an owner retry', async () => {
  const queryClient = new QueryClient()
  let calls = 0
  const data = await queryClient.fetchInfiniteQuery({
    queryKey: chatActivityQueryKey('chat'),
    initialPageParam: null,
    queryFn: async () => {
      calls += 1
      if (calls < 3) throw new TypeError('Failed to fetch')
      return { events: [], next_before: null }
    },
    retry: retryChatActivity,
    retryDelay: 0,
  })
  assert.equal(calls, 3)
  assert.deepEqual(data.pages, [{ events: [], next_before: null }])
  queryClient.clear()
})
