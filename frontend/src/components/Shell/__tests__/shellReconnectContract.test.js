// Exercise the owning live-list read against real query cancellation and a controlled API.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient } from '@tanstack/react-query'
import { api } from '../../../api/client.js'
import { fetchFreshChatList } from '../chatListReconciliation.js'

function freshReader(t, queryClient, fetch) {
  t.mock.method(api.chats, 'list', async options => ({
    ok: true,
    json: async () => fetch(options),
  }))
  return options => fetchFreshChatList(queryClient, { timeoutMs: 5000, ...options })
}

test('fresh chat reconciliation requests live-only truth and replaces the old cached row', async t => {
  const client = new QueryClient()
  client.setQueryData(['chats'], [{ pending_question_id: 'answered' }])
  let options
  const fresh = freshReader(t, client, async value => { options = value; return [{ pending_question_id: null }] })
  try {
    await fresh()
    assert.equal(options.cache, 'no-store')
    assert.equal(options.timeoutMs, 5000)
    assert.equal(options.signal.aborted, false)
    assert.equal(client.getQueryData(['chats'])[0].pending_question_id, null)
  } finally { client.clear() }
})

test('an aborted reconnect cannot commit late list data over a newer projection', async t => {
  const client = new QueryClient()
  const controller = new AbortController()
  let release, receivedSignal
  const fresh = freshReader(t, client, options => {
    receivedSignal = options.signal
    return new Promise(resolve => { release = resolve })
  })
  try {
    const pending = fresh({ signal: controller.signal })
    const rejected = assert.rejects(pending, error => error.name === 'AbortError')
    while (!release) await Promise.resolve()
    controller.abort()
    client.setQueryData(['chats'], [{ pending_question_id: 'new-question' }])
    release([{ pending_question_id: 'obsolete-question' }])
    await rejected
    assert.equal(receivedSignal.aborted, true)
    assert.equal(client.getQueryData(['chats'])[0].pending_question_id, 'new-question')
  } finally { client.clear() }
})

test('abort during query cancellation cannot start an obsolete replacement read', async t => {
  const controller = new AbortController()
  let finishCancel, fetches = 0
  const fresh = freshReader(t, {
    cancelQueries: () => new Promise(resolve => { finishCancel = resolve }),
    fetchQuery: () => { fetches++; return Promise.resolve([]) },
  }, () => [])
  const pending = fresh({ signal: controller.signal })
  const rejected = assert.rejects(pending, error => error.name === 'AbortError')
  controller.abort(); finishCancel(); await rejected
  assert.equal(fetches, 0)
})
