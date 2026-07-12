import test from 'node:test'
import assert from 'node:assert/strict'

import { resolveInitialNav } from '../resolveInitialNav.js'

// resolveInitialNav is pure — no globals needed. It resolves the initial view
// AND whether HOME must be seeded beneath it as the back-stack root. The
// invariant under test: a DEEP initial entry (notification deep-link,
// cold-restore, shell-reload into a non-chat view) seeds HOME so Back can always
// reach the chat surface; plain home (and a shell-reload that restored chat)
// does NOT seed (else Back from ordinary home would no-op instead of exiting).

test('no sources → plain home, no seed', () => {
  const r = resolveInitialNav({ storedChatId: 'chat-1' })
  assert.deepEqual(r, { view: 'chat', appId: null, chatId: 'chat-1', seedHome: false })
})

test('no sources, no stored chat → home with null chat, no seed', () => {
  const r = resolveInitialNav({})
  assert.deepEqual(r, { view: 'chat', appId: null, chatId: null, seedHome: false })
})

test('deep-link to an app (the reflection-report case) → canvas + seedHome', () => {
  const r = resolveInitialNav({ deepLink: { view: 'canvas', appId: 56 }, storedChatId: 'chat-1' })
  assert.equal(r.view, 'canvas')
  assert.equal(r.appId, 56)
  assert.equal(r.seedHome, true)
  // Keeps the stored chat loaded in the background so Back-to-home has a target.
  assert.equal(r.chatId, 'chat-1')
})

test('cold-restore of last-viewed app → canvas + seedHome', () => {
  const r = resolveInitialNav({ restored: { view: 'canvas', appId: 7 }, storedChatId: 'chat-9' })
  assert.equal(r.view, 'canvas')
  assert.equal(r.appId, 7)
  assert.equal(r.seedHome, true)
  assert.equal(r.chatId, 'chat-9')
})

test('return-view settings → settings + seedHome, no app', () => {
  const r = resolveInitialNav({ returnView: { view: 'settings' }, storedChatId: 'chat-1' })
  assert.equal(r.view, 'settings')
  assert.equal(r.appId, null)
  assert.equal(r.seedHome, true)
})

test('deep-link to a DIFFERENT chat than home → seedHome', () => {
  const r = resolveInitialNav({ deepLink: { view: 'chat', chatId: 'chat-X' }, storedChatId: 'chat-home' })
  assert.equal(r.view, 'chat')
  assert.equal(r.chatId, 'chat-X')
  assert.equal(r.seedHome, true)
})

test('deep-link to the SAME chat as home → no seed (that chat IS home)', () => {
  const r = resolveInitialNav({ deepLink: { view: 'chat', chatId: 'chat-home' }, storedChatId: 'chat-home' })
  assert.equal(r.view, 'chat')
  assert.equal(r.chatId, 'chat-home')
  assert.equal(r.seedHome, false)
})

test('shell-reload that restored the CHAT surface → no seed', () => {
  const r = resolveInitialNav({
    shellReload: { activeView: 'chat', activeChatId: 'chat-2' },
    storedChatId: 'chat-2',
  })
  assert.equal(r.view, 'chat')
  assert.equal(r.chatId, 'chat-2')
  assert.equal(r.seedHome, false)
})

test('shell-reload that restored a CANVAS → seedHome (lost navStack, needs a root)', () => {
  const r = resolveInitialNav({
    shellReload: { activeView: 'canvas', activeAppId: 3, activeChatId: 'chat-2' },
    storedChatId: 'chat-2',
  })
  assert.equal(r.view, 'canvas')
  assert.equal(r.appId, 3)
  assert.equal(r.seedHome, true)
  assert.equal(r.chatId, 'chat-2')
})

test('precedence: shellReload beats deepLink beats returnView beats restored', () => {
  const all = {
    shellReload: { activeView: 'canvas', activeAppId: 1 },
    deepLink: { view: 'chat', chatId: 'd' },
    returnView: { view: 'settings' },
    restored: { view: 'canvas', appId: 2 },
    storedChatId: 's',
  }
  assert.equal(resolveInitialNav(all).appId, 1) // shellReload wins

  const noShell = { ...all, shellReload: null }
  assert.equal(resolveInitialNav(noShell).view, 'chat') // deepLink wins
  assert.equal(resolveInitialNav(noShell).chatId, 'd')

  const noDeep = { ...noShell, deepLink: null }
  assert.equal(resolveInitialNav(noDeep).view, 'settings') // returnView wins

  const onlyRestored = { ...noDeep, returnView: null }
  assert.equal(resolveInitialNav(onlyRestored).appId, 2) // restored wins
})

test('canvas chatId falls back to stored home chat when the source carries none', () => {
  const r = resolveInitialNav({ deepLink: { view: 'canvas', appId: 56 }, storedChatId: null })
  assert.equal(r.chatId, null)
  assert.equal(r.seedHome, true)
})
