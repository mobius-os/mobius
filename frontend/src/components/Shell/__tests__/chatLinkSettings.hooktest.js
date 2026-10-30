/* A chat link to a Settings section opens Settings there, like a notification tap. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useAppIntentNavigation from '../useAppIntentNavigation.js'

function harness() {
  const opened = []
  const navigations = []
  const { result } = renderHook(() => useAppIntentNavigation({
    appsRef: { current: [] },
    refreshApps: async () => [],
    notifyShell: () => {},
    setAppIntents: () => {},
    navToRef: { current: (...args) => navigations.push(args) },
    openSettingsSection: section => opened.push(section),
  }))
  return { nav: result.current.handleChatInternalNav, opened, navigations }
}

test('a waiting card link opens Settings at Storage and Memory', () => {
  const { nav, opened, navigations } = harness()
  nav(new URL('https://mobius.test/shell/?settings=storage'))
  assert.deepEqual(opened, ['storage'])
  assert.deepEqual(navigations, [])
})

test('an unknown Settings section opens Settings without a focus', () => {
  const { nav, opened } = harness()
  nav(new URL('https://mobius.test/shell/?settings=nonsense'))
  assert.deepEqual(opened, [null])
})

test('chat links still open chats', () => {
  const { nav, opened, navigations } = harness()
  nav(new URL('https://mobius.test/shell/?chat=abc'))
  assert.deepEqual(opened, [])
  assert.deepEqual(navigations, [['chat', { chatId: 'abc' }]])
})
