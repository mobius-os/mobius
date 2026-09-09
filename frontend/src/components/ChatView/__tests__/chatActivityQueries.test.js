/* Activity route and invalidation behavior stay exact-chat and read-only. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../../../api/client.js'
import {
  chatActivityQueryKey,
  invalidateAllChatActivity,
  invalidateChatActivityForSystemEvent,
} from '../chatActivityQueries.js'

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
