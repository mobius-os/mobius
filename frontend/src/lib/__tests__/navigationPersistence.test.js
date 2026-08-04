import assert from 'node:assert/strict'
import test from 'node:test'

import {
  consumeReturnView,
  parseShellDeepLink,
  persistActiveNavigation,
  readRecentlyOpenedChatIds,
  readRestoredCanvas,
  readStoredChatId,
} from '../navigationPersistence.js'

function storage(seed = {}) {
  const values = new Map(Object.entries(seed))
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    values,
  }
}

test('cold restore reads one coherent app destination', () => {
  const store = storage({
    moebius_active_chat: 'chat-1',
    moebius_active_view: 'canvas',
    moebius_active_app: '42',
  })
  assert.equal(readStoredChatId(store), 'chat-1')
  assert.deepEqual(readRestoredCanvas(store), { view: 'canvas', appId: 42 })
})

test('deep links preserve slug, numeric identity, and intent', () => {
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/', search: '?app=42&intent=open%3Areport',
  }), { view: 'canvas', app: '42', appId: 42, intent: 'open:report' })
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/', search: '?app=artifacts',
  }), { view: 'canvas', app: 'artifacts', appId: null, intent: null })
})

test('return-view is consumed once', () => {
  const store = storage({ 'mobius:return-view': 'settings' })
  assert.deepEqual(consumeReturnView(store), { view: 'settings' })
  assert.equal(consumeReturnView(store), null)
})

test('active navigation mirrors cold state without retaining a stale app', () => {
  const store = storage({ moebius_active_app: '9' })
  persistActiveNavigation(store, {
    activeView: 'canvas', activeChatId: 'chat-2', activeAppId: 7,
  })
  assert.equal(store.values.get('moebius_active_chat'), 'chat-2')
  assert.equal(store.values.get('moebius_active_view'), 'canvas')
  assert.equal(store.values.get('moebius_active_app'), '7')

  persistActiveNavigation(store, {
    activeView: 'chat', activeChatId: 'chat-2', activeAppId: null,
  })
  assert.equal(store.values.has('moebius_active_app'), false)
})

test('recent chat history is device-local, deduplicated, and bounded', () => {
  const store = storage()
  for (let index = 0; index < 14; index += 1) {
    persistActiveNavigation(store, {
      activeView: 'chat', activeChatId: `chat-${index}`, activeAppId: null,
    })
  }
  persistActiveNavigation(store, {
    activeView: 'chat', activeChatId: 'chat-7', activeAppId: null,
  })

  const recent = readRecentlyOpenedChatIds(store)
  assert.equal(recent.length, 12)
  assert.equal(recent[0], 'chat-7')
  assert.equal(recent.filter(id => id === 'chat-7').length, 1)
})

test('only a visible chat navigation records an open', () => {
  const store = storage()
  persistActiveNavigation(store, {
    activeView: 'canvas', activeChatId: 'remembered-chat', activeAppId: 7,
  })
  assert.deepEqual(readRecentlyOpenedChatIds(store), [])

  persistActiveNavigation(store, {
    activeView: 'chat', activeChatId: 'visible-chat', activeAppId: null,
  })
  assert.deepEqual(readRecentlyOpenedChatIds(store), ['visible-chat'])
})

test('malformed recent chat history degrades to no history', () => {
  const store = storage({ 'mobius:recent-chat-ids': '{bad' })
  assert.deepEqual(readRecentlyOpenedChatIds(store), [])
})
