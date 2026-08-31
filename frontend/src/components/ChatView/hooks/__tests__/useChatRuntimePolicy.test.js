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

test('a provisional chat adopts its created runtime policy without remounting', () => {
  resetProviderSwitchMemoryForTests()
  const props = {
    chatId: 'policy-provisional',
    cached: null,
    hidden: false,
    onProviderSwitchSettled() {},
    request: unusedRequest,
  }
  const { result, rerender } = renderHook(useChatRuntimePolicy, props)
  assert.equal(result.current.chatInfo, null)

  rerender({
    ...props,
    cached: {
      chatInfo: {
        provider: 'codex',
        auto_resume_on_limit: false,
        effective: { model: 'gpt-current' },
      },
    },
  })

  assert.equal(result.current.chatInfo.provider, 'codex')
  assert.deepEqual(result.current.chatInfo.effective, { model: 'gpt-current' })
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

test('a settled first turn adopts its provider session without a remount', () => {
  resetProviderSwitchMemoryForTests()
  const initial = {
    chatInfo: {
      provider: 'claude',
      session_id: null,
      effective: { model: 'claude-opus-4-8' },
    },
  }
  const { result, rerender } = renderHook(useChatRuntimePolicy, {
    chatId: 'policy-first-turn',
    cached: initial,
    hidden: false,
    onProviderSwitchSettled() {},
    request: unusedRequest,
  })

  assert.equal(result.current.chatInfo.session_id, null)

  rerender({
    chatId: 'policy-first-turn',
    cached: {
      chatInfo: {
        ...initial.chatInfo,
        session_id: 'claude-session-1',
      },
    },
    hidden: false,
    onProviderSwitchSettled() {},
    request: unusedRequest,
  })

  assert.equal(result.current.chatInfo.session_id, 'claude-session-1')
  assert.deepEqual(
    result.current.chatInfo.effective,
    { model: 'claude-opus-4-8' },
  )
})

test('a late session from the outgoing provider is never adopted', () => {
  resetProviderSwitchMemoryForTests()
  const { result, rerender } = renderHook(useChatRuntimePolicy, {
    chatId: 'policy-provider-bound-session',
    cached: {
      chatInfo: { provider: 'codex', session_id: null },
    },
    hidden: false,
    onProviderSwitchSettled() {},
    request: unusedRequest,
  })

  result.current.mergeChatInfo({
    provider: 'claude',
    agent_settings_json: { model: 'claude-opus-4-8' },
    effective: { model: 'claude-opus-4-8' },
  })
  rerender({
    chatId: 'policy-provider-bound-session',
    cached: {
      chatInfo: { provider: 'codex', session_id: 'codex-thread-late' },
    },
    hidden: false,
    onProviderSwitchSettled() {},
    request: unusedRequest,
  })

  assert.equal(result.current.chatInfo.provider, 'claude')
  assert.equal(result.current.chatInfo.session_id, null)
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
