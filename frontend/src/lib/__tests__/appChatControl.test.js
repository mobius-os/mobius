import test from 'node:test'
import assert from 'node:assert/strict'

import { makeAppChatAuthorizer, makeAppChatController } from '../appChatControl.js'

test('app chat authorizer accepts known ownership without a detail read', async () => {
  let reads = 0
  const authorize = makeAppChatAuthorizer({
    knownChats: () => [{ id: 'chat-1', created_by_app_id: 80 }],
    loadChat: async () => { reads += 1; return null },
  })
  assert.equal(await authorize(80, 'chat-1'), true)
  assert.equal(reads, 0)
})

test('app chat authorizer verifies an off-list chat once then caches ownership', async () => {
  let reads = 0
  const authorize = makeAppChatAuthorizer({
    knownChats: () => [],
    loadChat: async (id) => { reads += 1; return { id, created_by_app_id: 80 } },
  })
  assert.equal(await authorize(80, 'chat-2'), true)
  assert.equal(await authorize(80, 'chat-2'), true)
  assert.equal(reads, 1)
})

test('app chat authorizer rejects cross-app control', async () => {
  const authorize = makeAppChatAuthorizer({
    knownChats: () => [{ id: 'chat-3', created_by_app_id: 81 }],
  })
  await assert.rejects(authorize(80, 'chat-3'), /does not belong/)
})


test('app chat authorizer rejects ownership returned by a detail read', async () => {
  const authorize = makeAppChatAuthorizer({
    knownChats: () => [],
    loadChat: async (id) => ({ id, created_by_app_id: 81 }),
  })
  await assert.rejects(authorize(80, 'chat-4'), /does not belong/)
})

test('app chat controller shares status enrichment and stop semantics', async () => {
  const calls = []
  const control = makeAppChatController({
    knownChats: () => [{ id: 'chat-5', created_by_app_id: 80 }],
    chats: {
      detail: async () => { throw new Error('known chat should not be loaded') },
      runtime: async (id, options) => { calls.push(['runtime', id, options]); return { kind: 'runtime' } },
      goalPlan: async (id, options) => {
        calls.push(['goal', id, options])
        return { ok: true, json: async () => ({ plan: { current: 'review' } }) }
      },
      usage: async (id, options) => {
        calls.push(['usage', id, options])
        return { ok: true, json: async () => ({ totals: { total_tokens: 1200 } }) }
      },
      stop: async (id, options) => { calls.push(['stop', id, options]); return { kind: 'stop' } },
    },
    readJson: async value => value,
  })

  assert.deepEqual(await control(80, { action: 'status', chatId: 'chat-5' }), {
    kind: 'runtime',
    goal_plan: { current: 'review' },
    usage: { totals: { total_tokens: 1200 } },
  })
  assert.deepEqual(await control(80, { action: 'stop', chatId: 'chat-5' }), { kind: 'stop' })
  assert.deepEqual(calls, [
    ['goal', 'chat-5', { timeoutMs: 5000 }],
    ['usage', 'chat-5', { timeoutMs: 5000 }],
    ['runtime', 'chat-5', { timeoutMs: 5000 }],
    ['stop', 'chat-5', { timeoutMs: 15000 }],
  ])
})
