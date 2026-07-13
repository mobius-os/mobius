import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createProviderSwitchId,
  providerSwitchPayload,
  providerSwitchResponseData,
  restorableProviderSwitch,
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
  })
  assert.equal(data, null)
})
