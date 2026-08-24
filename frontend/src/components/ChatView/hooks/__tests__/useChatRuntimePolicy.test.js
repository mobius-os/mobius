import assert from 'node:assert/strict'
import test from 'node:test'

import {
  completeProviderSwitch,
  getProviderSwitchState,
  resetProviderSwitchMemoryForTests,
} from '../../providerSwitch.js'
import useChatRuntimePolicy from '../useChatRuntimePolicy.js'
import { renderHook } from './react-hook-shim.mjs'

const unusedRequest = async () => {
  throw new Error('unexpected request')
}

test('runtime policy derives resumability from the cached chat contract', () => {
  resetProviderSwitchMemoryForTests()
  const { result } = renderHook(useChatRuntimePolicy, {
    chatId: 'policy-cached',
    cached: {
      chatInfo: {
        provider: 'codex',
        auto_resume_on_limit: true,
      },
    },
    hidden: false,
    onProviderSwitchSettled() {},
    request: unusedRequest,
  })

  assert.equal(result.current.chatInfo.provider, 'codex')
  assert.equal(result.current.autoResumeEnabled, true)
})

test('a completed provider switch settles through one owner and clears its store', () => {
  resetProviderSwitchMemoryForTests()
  const chatId = 'policy-switch'
  let settled = 0
  completeProviderSwitch(chatId, { switch_id: 'switch-1' }, {
    provider: 'claude',
    agent_settings_json: { model: 'sonnet' },
    effective: { model: 'sonnet' },
  })

  const { result } = renderHook(useChatRuntimePolicy, {
    chatId,
    cached: {
      chatInfo: {
        provider: 'codex',
        agent_settings_json: {},
        effective: {},
      },
    },
    hidden: false,
    onProviderSwitchSettled() { settled += 1 },
    request: unusedRequest,
  })

  assert.equal(settled, 1)
  assert.equal(result.current.chatInfo.provider, 'claude')
  assert.deepEqual(result.current.chatInfo.effective, { model: 'sonnet' })
  assert.equal(getProviderSwitchState(chatId).status, 'idle')
})

test('chat-info patches preserve the provider when the response omits it', () => {
  resetProviderSwitchMemoryForTests()
  const { result } = renderHook(useChatRuntimePolicy, {
    chatId: 'policy-merge',
    cached: {
      chatInfo: {
        provider: 'codex',
        agent_settings_json: {},
        effective: {},
      },
    },
    hidden: false,
    onProviderSwitchSettled() {},
    request: unusedRequest,
  })

  result.current.mergeChatInfo({
    agent_settings_json: { effort: 'high' },
    effective: { effort: 'high' },
  })

  assert.equal(result.current.chatInfo.provider, 'codex')
  assert.deepEqual(result.current.chatInfo.effective, { effort: 'high' })
})

test('the auto-resume action persists the setting and updates one chat owner', async () => {
  resetProviderSwitchMemoryForTests()
  const calls = []
  const request = async (url, options) => {
    calls.push([url, options])
    const body = JSON.parse(options.body)
    return {
      ok: true,
      async json() { return body },
    }
  }
  const { result } = renderHook(useChatRuntimePolicy, {
    chatId: 'policy-save',
    cached: {
      chatInfo: {
        provider: 'codex',
        auto_resume_on_limit: false,
      },
    },
    hidden: false,
    onProviderSwitchSettled() {},
    request,
  })

  await result.current.handleAutoResumeSettingsChange(true)

  assert.equal(calls.length, 1)
  assert.equal(result.current.autoResumeEnabled, true)
  assert.equal(result.current.autoResumeSaving, false)
  result.current.clearAutoResumeError()
  assert.equal(result.current.autoResumeError, '')
})
