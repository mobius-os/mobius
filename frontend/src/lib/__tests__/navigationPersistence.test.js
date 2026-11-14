import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveInitialNav } from '../resolveInitialNav.js'
import { settingsDeepLink } from '../settingsSections.js'

import {
  consumeReturnView,
  parseShellDeepLink,
  persistActiveNavigation,
  previousPageDied,
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

test('a standalone app\'s Settings request opens that section, not the plain workspace', () => {
  // A standalone page answers moebius:open-settings by loading this link; the
  // workspace must read back the section the in-shell request would focus.
  const opened = section => {
    const href = settingsDeepLink(section)
    return { href, target: parseShellDeepLink(new URL(href, 'https://mobius.test')) }
  }
  assert.deepEqual(opened('mobius-account'), {
    href: '/shell/?settings=mobius-account',
    target: { view: 'settings', section: 'mobius-account' },
  })
  for (const unknown of ['retired-section', undefined]) {
    assert.deepEqual(opened(unknown), {
      href: '/shell/?settings=ai-providers',
      target: { view: 'settings', section: 'ai-providers' },
    }, 'an unknown section opens AI providers, as the in-shell request does')
  }
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

function pageEvents(visibilityState = 'visible') {
  const listeners = new Map()
  const target = () => ({
    addEventListener: (type, fn) => listeners.set(type, fn),
  })
  const win = target()
  const doc = { ...target(), visibilityState }
  const fire = (type) => listeners.get(type)?.()
  return { win, doc, fire }
}

test('a page the browser killed on screen is detected on the next load', () => {
  const session = storage()
  const first = pageEvents()
  assert.equal(previousPageDied(session, first.win, first.doc), false)
  // No pagehide/hidden/freeze: the browser killed the page and reloaded it.
  const reload = pageEvents()
  assert.equal(previousPageDied(session, reload.win, reload.doc), true)
})

test('a page that ends or goes to the background normally is not a crash', () => {
  for (const ending of ['pagehide', 'freeze', 'hidden']) {
    const session = storage()
    const page = pageEvents()
    previousPageDied(session, page.win, page.doc)
    if (ending === 'hidden') {
      page.doc.visibilityState = 'hidden'
      page.fire('visibilitychange')
    } else {
      page.fire(ending)
    }
    const next = pageEvents()
    assert.equal(previousPageDied(session, next.win, next.doc), false, ending)
  }
})

test('a page restored from the back-forward cache is watched again', () => {
  const session = storage()
  const page = pageEvents()
  previousPageDied(session, page.win, page.doc)
  page.fire('pagehide')
  page.fire('pageshow')
  const next = pageEvents()
  assert.equal(previousPageDied(session, next.win, next.doc), true)
})

test('crash detection stays off when session storage is unavailable', () => {
  const blocked = {
    getItem() { throw new DOMException('Browser storage is blocked.', 'SecurityError') },
    setItem() { throw new DOMException('Browser storage is blocked.', 'SecurityError') },
    removeItem() { throw new DOMException('Browser storage is blocked.', 'SecurityError') },
  }
  const page = pageEvents()
  assert.equal(previousPageDied(blocked, page.win, page.doc), false)
  assert.doesNotThrow(() => page.fire('pagehide'))
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

test('result deep links identify only a project and artifact, without changing project links', () => {
  assert.deepEqual(parseShellDeepLink({ pathname: '/shell/', search: '?project=project-7&result=website_2' }),
    { view: 'artifact', projectId: 'project-7', artifactId: 'website_2' })
  for (const search of ['?project=p&result=', '?project=p&result=a%3Ab', '?project=p&result=..%2Fsecret', '?project=p&result=' + 'a'.repeat(65)]) {
    assert.deepEqual(parseShellDeepLink({ pathname: '/shell/', search }), { view: 'project', projectId: 'p' })
  }
  assert.equal(parseShellDeepLink({ pathname: '/shell/', search: '?result=site' }), null)
  assert.deepEqual(parseShellDeepLink({ pathname: '/shell/', search: '?project=p%3Aother&result=site' }),
    { view: 'project', projectId: 'p:other' })
})
