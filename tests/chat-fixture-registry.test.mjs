import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  drainCreatedChats,
  registerCreatedChats,
} from './_chatFixtureRegistry.mjs'
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
