import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  drainCreatedChats,
  registerCreatedChats,
} from './_chatFixtureRegistry.mjs'
import { createTaggedChat } from './_chatTracker.mjs'
import {
  installMockAgentProvider,
  persistTestChatModel,
  testChatAgentSettings,
  TEST_CHAT_MODEL,
} from './_chatTestPrerequisites.mjs'

test('registry drains only exact IDs registered by one worker', () => {
  registerCreatedChats(3, ['chat-a', { id: 'chat-b' }, 'chat-a', null])
  registerCreatedChats(4, 'other-worker-chat')

  assert.deepEqual(drainCreatedChats(3), ['chat-a', 'chat-b'])
  assert.deepEqual(drainCreatedChats(3), [])
  assert.deepEqual(drainCreatedChats(4), ['other-worker-chat'])
})

test('created chats are registered before model setup can fail', async () => {
  const source = await readFile(
    new URL('./_chatTracker.mjs', import.meta.url),
    'utf8',
  )
  const createStart = source.indexOf('export async function createTaggedChat')
  const cleanupStart = source.indexOf('export async function cleanupWorkerChats')
  const createSource = source.slice(createStart, cleanupStart)
  const registerIndex = createSource.indexOf(
    'registerCreatedChats(info.workerIndex, result.id)',
  )
  const modelSetupIndex = createSource.indexOf('persistTestChatModel(page')

  assert.ok(registerIndex >= 0)
  assert.ok(modelSetupIndex >= 0)
  assert.ok(registerIndex < modelSetupIndex)
})

test('fixture setup failures include a bounded backend response', async () => {
  const response = ({ ok, status, body, json }) => ({
    ok: () => ok,
    status: () => status,
    text: async () => body,
    json: async () => json,
  })
  const requests = []
  const page = {
    evaluate: async () => 'fixture-token',
    request: {
      post: async () => requests.shift(),
      patch: async () => requests.shift(),
    },
  }

  requests.push(response({
    ok: false,
    status: 500,
    body: 'database transaction failed',
  }))
  await assert.rejects(
    createTaggedChat(page, 'create-failure', { mockProvider: false }),
    /Could not create the test chat \(500\): database transaction failed/,
  )

  requests.push(
    response({ ok: true, status: 200, body: '', json: { id: 'chat-a' } }),
    response({ ok: false, status: 409, body: 'model choice conflicted' }),
  )
  await assert.rejects(
    createTaggedChat(page, 'model-failure', { mockProvider: false }),
    /Could not select the test chat model \(409\): model choice conflicted/,
  )

  requests.push(response({
    ok: false,
    status: 502,
    body: `${'x'.repeat(1200)}tail`,
  }))
  await assert.rejects(
    createTaggedChat(page, 'bounded-failure', { mockProvider: false }),
    error => {
      const detail = error.message.split(': ', 2)[1]
      assert.equal(detail.length, 1000)
      assert.doesNotMatch(error.message, /tail/)
      return true
    },
  )
})

test('chat fixtures persist a model and simulate its provider boundary', async () => {
  const calls = []
  const routes = []
  const response = body => ({
    ok: () => true,
    json: async () => body,
    status: () => 200,
  })
  const page = {
    route: async (pattern, handler) => routes.push({ pattern, handler }),
    request: {
      patch: async (url, options) => {
        calls.push({ method: 'PATCH', url, options })
        return response({ ok: true })
      },
    },
  }

  await installMockAgentProvider(page)
  const selected = await persistTestChatModel(page, {
    base: 'http://localhost:8001',
    chatId: 'fixture-chat',
    token: 'fixture-token',
  })

  assert.equal(selected.ok(), true)
  assert.equal(routes.length, 1)
  assert.equal(routes[0].pattern, '**/api/auth/providers/status')
  assert.deepEqual(calls.map(call => call.method), ['PATCH'])
  assert.deepEqual(calls[0].options.data, {
    agent_settings_json: { model: TEST_CHAT_MODEL },
  })
  assert.deepEqual(testChatAgentSettings(), {
    agent_settings_json: { model: TEST_CHAT_MODEL },
    effective_agent_settings: {
      model: TEST_CHAT_MODEL,
      effort: 'medium',
    },
  })
})
