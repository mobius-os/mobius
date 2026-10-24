import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveInitialNav } from '../resolveInitialNav.js'

import {
  consumeReturnView,
  parseShellDeepLink,
  persistActiveNavigation,
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
    pathname: '/shell/shared', search: '?chat=guest-chat&focus=question',
  }), { view: 'chat', chatId: 'guest-chat', intent: null, focusQuestion: true })
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/shared/', search: '?app=42&intent=open%3Areport',
  }), { view: 'canvas', app: '42', appId: 42, intent: 'open:report' })
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/', search: '?app=42&intent=open%3Areport',
  }), { view: 'canvas', app: '42', appId: 42, intent: 'open:report' })
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/', search: '?app=pages',
  }), { view: 'canvas', app: 'pages', appId: null, intent: null })
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/', search: '?chat=chat-1&focus=question',
  }), { view: 'chat', chatId: 'chat-1', intent: null, focusQuestion: true })
})

test('a /shell/?settings deep link opens Settings at a known section only', () => {
  assert.deepEqual(parseShellDeepLink({ pathname: '/shell/', search: '?settings=github' }),
    { view: 'settings', section: 'github' })
  assert.deepEqual(parseShellDeepLink({ pathname: '/shell/', search: '?settings' }),
    { view: 'settings' })
  assert.deepEqual(parseShellDeepLink({ pathname: '/shell/', search: '?settings=evil' }),
    { view: 'settings' })
  assert.equal(parseShellDeepLink({ pathname: '/shell/', search: '?chat=c&settings=github' }).view,
    'chat')
})

test('legacy path deep links translate /app, /chat, and /settings', () => {
  // The backend serves the shell for these bare paths (agent-screenshot.sh
  // documents /app/<id>; the AI-provider OAuth callback redirects to /settings),
  // so parseShellDeepLink must translate them into a boot destination in the
  // same shape as the /shell/?app=/?chat= forms. #1119 dropped these and made
  // /app/<id> and /settings land on the home screen.
  assert.deepEqual(parseShellDeepLink({ pathname: '/app/118', search: '' }),
    { view: 'canvas', app: '118', appId: 118, intent: null })
  assert.deepEqual(parseShellDeepLink({ pathname: '/app/pages/', search: '' }),
    { view: 'canvas', app: 'pages', appId: null, intent: null })
  assert.deepEqual(parseShellDeepLink({ pathname: '/chat/c-1', search: '' }),
    { view: 'chat', chatId: 'c-1', intent: null, focusQuestion: false })
  assert.deepEqual(parseShellDeepLink({ pathname: '/settings', search: '?section=ai-providers' }),
    { view: 'settings', section: 'ai-providers' })
  assert.deepEqual(parseShellDeepLink({ pathname: '/settings/', search: '' }),
    { view: 'settings' })
  // A non-legacy single segment is still an ordinary empty destination.
  assert.equal(parseShellDeepLink({ pathname: '/app', search: '' }), null)
})

test('OAuth success and error callbacks mark only the allowed Settings section', () => {
  for (const search of [
    '?section=ai-providers&mobius_enroll_return=1',
    '?section=ai-providers&mobius_enroll_error=1',
  ]) {
    assert.deepEqual(parseShellDeepLink({ pathname: '/settings', search }),
      { view: 'settings', section: 'ai-providers', providerReturn: true })
  }
  assert.deepEqual(parseShellDeepLink({ pathname: '/settings', search: '?section=ai-providers' }),
    { view: 'settings', section: 'ai-providers' })
  for (const search of [
    '?section=unknown',
    '?section=%23settings-ai-providers',
    '?section=ai-providers&section=unknown',
    '?mobius_enroll_error=1',
    '?section=ai-providers&mobius_enroll_return=1&mobius_enroll_return=1',
  ]) {
    assert.equal(parseShellDeepLink({ pathname: '/settings', search }).providerReturn, undefined)
  }
})

test('cold OAuth callback beats stale reload and retains the focus section', () => {
  const shellReload = { activeView: 'canvas', activeAppId: 42, activeChatId: 'chat-1', destinationClaimed: true }
  for (const search of [
    '?section=ai-providers&mobius_enroll_return=1',
    '?section=ai-providers&mobius_enroll_error=1',
  ]) {
    const deepLink = parseShellDeepLink({ pathname: '/settings', search })
    assert.deepEqual(resolveInitialNav({ shellReload, deepLink, storedChatId: 'chat-1' }), {
      view: 'settings', appId: null, chatId: 'chat-1', seedHome: true, section: 'ai-providers',
    })
  }
  const ordinary = parseShellDeepLink({ pathname: '/settings', search: '?section=ai-providers' })
  assert.equal(resolveInitialNav({ shellReload, deepLink: ordinary }).appId, 42)
  assert.equal(resolveInitialNav({ deepLink: ordinary }).section, 'ai-providers')
})

test('deep links can open the Projects directory or one project', () => {
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/', search: '?projects=1',
  }), { view: 'projects' })
  assert.deepEqual(parseShellDeepLink({
    pathname: '/shell/', search: '?project=project-7',
  }), { view: 'project', projectId: 'project-7' })
})

test('return-view is consumed once', () => {
  const store = storage({ 'mobius:return-view': 'settings' })
  assert.deepEqual(consumeReturnView(store), { view: 'settings' })
  assert.equal(consumeReturnView(store), null)
})

test('cold navigation ignores browser storage getters that throw SecurityError', (t) => {
  for (const key of ['localStorage', 'sessionStorage']) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, key)
    t.after(() => {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    })
    Object.defineProperty(globalThis, key, {
      configurable: true,
      get() { throw new DOMException('Browser storage is blocked.', 'SecurityError') },
    })
  }

  assert.equal(readStoredChatId(), null)
  assert.equal(readRestoredCanvas(), null)
  assert.equal(consumeReturnView(), null)
})

test('cold navigation ignores storage operations that are unavailable', () => {
  const blocked = {
    getItem() { throw new DOMException('Browser storage is blocked.', 'SecurityError') },
  }
  assert.equal(readStoredChatId(blocked), null)
  assert.equal(readRestoredCanvas(blocked), null)
  assert.equal(consumeReturnView(blocked), null)
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

test('decision link retains the exact question identity, not just current input focus', () => {
  assert.deepEqual(parseShellDeepLink({ pathname: '/shell/', search: '?chat=source&focus=question&question=saved-card' }),
    { view: 'chat', chatId: 'source', intent: null, focusQuestion: true, questionId: 'saved-card' })
})
