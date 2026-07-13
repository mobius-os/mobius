import assert from 'node:assert/strict'
import test from 'node:test'

import {
  beginProviderSwitch,
  clearProviderSwitch,
  createProviderSwitchId,
  getProviderSwitchState,
  isProviderSwitchBlocking,
  providerSwitchPayload,
  providerSwitchResponseData,
  resetProviderSwitchMemoryForTests,
  restorableProviderSwitch,
  stageProviderSwitch,
  subscribeProviderSwitch,
} from '../providerSwitch.js'

test('providerSwitchPayload carries the incoming model and stable request id', () => {
  const payload = providerSwitchPayload({
    provider: 'codex',
    model: 'gpt-5.4',
    effort: 'high',
    effortByProvider: { claude: 'medium', codex: 'high' },
    switchId: 'switch-stable',
  })

  assert.deepEqual(payload, {
    provider: 'codex',
    switch_id: 'switch-stable',
    agent_settings_json: {
      model: 'gpt-5.4',
      effort: 'high',
      effort_by_provider: { claude: 'medium', codex: 'high' },
    },
  })
})

test('createProviderSwitchId returns a non-empty retry identity', () => {
  assert.match(createProviderSwitchId(), /\S+/)
})

test('provider switch retry identity survives a panel remount', () => {
  const saved = {
    chatId: 'chat-1',
    sourceProvider: 'claude',
    provider: 'codex',
    model: 'gpt-5.4',
    switchId: 'stable-after-close',
  }

  assert.equal(
    restorableProviderSwitch(saved, 'chat-1', 'claude'),
    saved,
  )
  assert.equal(restorableProviderSwitch(saved, 'chat-2', 'claude'), null)
  assert.equal(restorableProviderSwitch(saved, 'chat-1', 'codex'), null)
})

test('an unreadable 2xx body remains ambiguous instead of clearing retry state', async () => {
  const data = await providerSwitchResponseData({
    async json() {
      throw new Error('response body interrupted')
    },
  }, { provider: 'codex', switchId: 'switch-1' })
  assert.equal(data, null)
})

test('only the versioned response for this request is authoritative', async () => {
  const response = body => ({ async json() { return body } })
  const expected = { provider: 'codex', switchId: 'switch-1' }

  assert.equal(await providerSwitchResponseData(response({ ok: true }), expected), null)
  assert.equal(await providerSwitchResponseData(response({
    protocol: 'provider-switch-v1',
    switch_id: 'other-request',
    provider: 'codex',
  }), expected), null)
  assert.equal(await providerSwitchResponseData(response({
    protocol: 'provider-switch-v1',
    switch_id: 'switch-1',
    provider: 'claude',
  }), expected), null)

  const valid = {
    protocol: 'provider-switch-v1',
    switch_id: 'switch-1',
    provider: 'codex',
  }
  assert.deepEqual(
    await providerSwitchResponseData(response(valid), expected),
    valid,
  )
})

test('per-chat switching state survives a ChatView remount and blocks sends', () => {
  resetProviderSwitchMemoryForTests()
  const request = {
    chatId: 'chat-remount',
    sourceProvider: 'claude',
    provider: 'codex',
    switchId: 'stable-remount-id',
  }
  let notifications = 0
  const unsubscribe = subscribeProviderSwitch(
    request.chatId,
    () => { notifications += 1 },
  )

  stageProviderSwitch(request.chatId, request)
  beginProviderSwitch(request.chatId, request)
  assert.equal(getProviderSwitchState(request.chatId).request, request)
  assert.equal(getProviderSwitchState(request.chatId).status, 'switching')
  assert.equal(isProviderSwitchBlocking(request.chatId), true)
  assert.equal(notifications, 2)

  // A new subscriber is the store-level equivalent of the keyed ChatView
  // mounting again after A → B → A navigation.
  unsubscribe()
  assert.equal(getProviderSwitchState(request.chatId).request.switchId, 'stable-remount-id')
  clearProviderSwitch(request.chatId)
})

test('reload turns an orphaned in-flight request into an idempotent retry', () => {
  const items = new Map()
  const previousStorage = globalThis.sessionStorage
  globalThis.sessionStorage = {
    getItem: key => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value),
    removeItem: key => items.delete(key),
  }
  try {
    resetProviderSwitchMemoryForTests()
    const request = {
      chatId: 'chat-reload',
      sourceProvider: 'claude',
      provider: 'codex',
      switchId: 'stable-reload-id',
    }
    beginProviderSwitch(request.chatId, request)
    resetProviderSwitchMemoryForTests()

    const restored = getProviderSwitchState(request.chatId)
    assert.equal(restored.status, 'error')
    assert.equal(restored.request.switchId, 'stable-reload-id')
    assert.match(restored.error, /may have completed/i)
  } finally {
    resetProviderSwitchMemoryForTests()
    if (previousStorage === undefined) delete globalThis.sessionStorage
    else globalThis.sessionStorage = previousStorage
  }
})
