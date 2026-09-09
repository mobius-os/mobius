import test from 'node:test'
import assert from 'node:assert/strict'
import { consumeChatChanges, requestChatChanges, subscribeChatChanges } from '../chatChangesNavigation.js'

function installStorage(t, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value })
  t.after(() => previous ? Object.defineProperty(globalThis, 'sessionStorage', previous) : delete globalThis.sessionStorage)
}

function storage() {
  const data = new Map()
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) }
}

test('Changes opens only the requested chat once, without a composer or preparation action', t => {
  installStorage(t, storage())
  let source = 0, unrelated = 0
  const stopSource = subscribeChatChanges('source', () => { if (consumeChatChanges('source')) source++ })
  const stopOther = subscribeChatChanges('other', () => { unrelated++ })
  requestChatChanges('source')
  assert.equal(source, 1); assert.equal(unrelated, 0)
  assert.equal(consumeChatChanges('source'), false)
  stopSource(); stopOther()
})

test('an unmounted target retains the one-shot until its own Changes surface mounts', t => {
  installStorage(t, storage())
  requestChatChanges('unmounted')
  assert.equal(consumeChatChanges('unrelated'), false)
  assert.equal(consumeChatChanges('unmounted'), true)
  assert.equal(consumeChatChanges('unmounted'), false)
})

test('standalone navigation survives the document boundary and expires rather than reopening later', t => {
  const session = storage()
  installStorage(t, session)
  session.setItem('mobius-chat-changes-navigation', JSON.stringify({chatId:'standalone',expiresAt:Date.now()+60000}))
  assert.equal(consumeChatChanges('standalone'), true)
  session.setItem('mobius-chat-changes-navigation', JSON.stringify({chatId:'old',expiresAt:Date.now()-1}))
  assert.equal(consumeChatChanges('old'), false)
  assert.equal(session.getItem('mobius-chat-changes-navigation'), null)
})

test('restricted storage does not break same-document Changes navigation', t => {
  installStorage(t, {getItem(){throw Error('blocked')},setItem(){throw Error('blocked')},removeItem(){throw Error('blocked')}})
  requestChatChanges('memory-only')
  assert.equal(consumeChatChanges('memory-only'), true)
})
