/* The invitation and workspace never use owner URL or storage state. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { consumeSharedBrowserEntry, watchSharedBrowserEntries } from '../sharedBrowserInvite.js'
import { isSharedBrowserRoute, sharedBrowserShellHref, sharedBrowserStorageForGrant, sharedBrowserWorkspaceStorage, setActiveSharedBrowserGrantId } from '../sharedBrowserWorkspace.js'
import { readAppFrameStorage, setAppFrameStorage } from '../appFrameStorage.js'
import { isOwnerWorkspace, localStore, ownerStore, sessionStore } from '../workspaceStorage.js'
import { persistActiveNavigation, readStoredChatId } from '../navigationPersistence.js'
import { resolveInitialNav } from '../resolveInitialNav.js'

test('invitation is returned once from a fragment and removed from the URL', () => {
  const calls = []
  const location = { hash: '#invite=secret%2Bvalue', pathname: '/shell/shared', search: '' }
  const history = { state: { x: 1 }, replaceState: (...args) => calls.push(args) }
  assert.deepEqual(consumeSharedBrowserEntry(location, history), { kind: 'invite', value: 'secret+value' })
  assert.deepEqual(calls, [[history.state, '', '/shell/shared']])
})

test('invitation is not read from a query parameter', () => {
  const history = { replaceState: () => assert.fail('should not rewrite URL') }
  assert.equal(consumeSharedBrowserEntry({ hash: '', pathname: '/shell/shared', search: '?invite=secret' }, history), null)
})

test('same-document invitation hash is stripped and admitted for fresh consent exactly once', () => {
  const listeners = new Map()
  const calls = []
  const location = { pathname: '/shell/shared', search: '', hash: '', href: 'https://mobius.test/shell/shared' }
  const history = { state: null, replaceState(_state, _title, path) {
    calls.push(path)
    location.hash = ''
    location.href = `https://mobius.test${path}`
  } }
  const win = {
    location, history,
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: (name, handler) => {
      if (listeners.get(name) === handler) listeners.delete(name)
    },
  }
  const acceptedForConsent = []
  const stop = watchSharedBrowserEntries(win, invite => acceptedForConsent.push(invite))
  location.hash = '#invite=second%2Bgrant'
  listeners.get('hashchange')()
  assert.deepEqual(calls, ['/shell/shared'])
  assert.deepEqual(acceptedForConsent, [{ kind: 'invite', value: 'second+grant' }])
  listeners.get('hashchange')()
  assert.deepEqual(acceptedForConsent, [{ kind: 'invite', value: 'second+grant' }])
  stop()
  assert.equal(listeners.has('hashchange'), false)
})

test('guest full-document shell links retain shared authority; external and unrelated links do not', () => {
  const current = { pathname: '/shell/shared', href: 'https://mobius.test/shell/shared' }
  assert.equal(sharedBrowserShellHref('/shell/?chat=c-1&repair=1', current),
    'https://mobius.test/shell/shared?chat=c-1&repair=1')
  assert.equal(sharedBrowserShellHref('https://mobius.test/shell/?app=42#view', current),
    'https://mobius.test/shell/shared?app=42#view')
  assert.equal(sharedBrowserShellHref('https://elsewhere.test/shell/?chat=c-1', current),
    'https://elsewhere.test/shell/?chat=c-1')
  assert.equal(sharedBrowserShellHref('/settings', current), '/settings')
  assert.equal(sharedBrowserShellHref('/shell/?chat=c-1', { pathname: '/shell/', href: 'https://mobius.test/shell/' }),
    '/shell/?chat=c-1')
})

test('shared workspaces are tab-scoped and partitioned by grant', () => {
  const values = new Map([['token', 'owner'], ['mobius-workspace', 'owner-workspace']])
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const first = sharedBrowserWorkspaceStorage('grant-1', storage)
  const second = sharedBrowserWorkspaceStorage('grant-2', storage)
  first.setItem('mobius-workspace', 'first')
  assert.equal(first.getItem('mobius-workspace'), 'first')
  assert.equal(second.getItem('mobius-workspace'), null)
  assert.equal(values.get('mobius-workspace'), 'owner-workspace')
  assert.equal(values.get('token'), 'owner')
})

test('reload restores the guest destination without consulting owner navigation', () => {
  const values = new Map([['moebius_active_chat', 'owner-chat']])
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
  const grantA = sharedBrowserWorkspaceStorage('grant-A', storage)
  const grantB = sharedBrowserWorkspaceStorage('grant-B', storage)
  persistActiveNavigation(grantA, { activeView: 'chat', activeChatId: 'guest-chat', activeAppId: null })
  const reloaded = resolveInitialNav({ storedChatId: readStoredChatId(grantA) })
  assert.equal(reloaded.chatId, 'guest-chat')
  assert.equal(readStoredChatId(grantB), null)
  assert.equal(values.get('moebius_active_chat'), 'owner-chat')
})

test('mini-app storage cannot see owner or another grant’s cache', () => {
  const values = new Map()
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.sessionStorage = {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
  globalThis.localStorage = {
    getItem: () => assert.fail('owner cache read'),
    setItem: () => assert.fail('owner cache write'),
  }
  setActiveSharedBrowserGrantId('grant-A')
  assert.equal(setAppFrameStorage('app-1', 'note', 'guest-A'), true)
  assert.equal(readAppFrameStorage('app-1').note, 'guest-A')
  // Renewal re-announces the same grant; caches keyed by the store survive it.
  const store = localStore()
  setActiveSharedBrowserGrantId('grant-A')
  assert.equal(localStore(), store)
  setActiveSharedBrowserGrantId('grant-B')
  assert.equal(readAppFrameStorage('app-1').note, undefined)
  setActiveSharedBrowserGrantId(null)
})

test('unavailable sessionStorage falls back to isolated document memory, never owner localStorage', () => {
  const oldDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const oldLocation = globalThis.location
  globalThis.location = { pathname: '/shell/shared/' }
  // A trailing slash is the same route, even without Web Storage.
  assert.equal(isSharedBrowserRoute('/shell/shared/'), true)
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    get() { throw new Error('blocked') },
  })
  try {
    const first = sharedBrowserStorageForGrant('blocked-grant')
    const second = sharedBrowserStorageForGrant('other-grant')
    first.setItem('workspace', 'guest-only')
    assert.equal(first.getItem('workspace'), 'guest-only')
    assert.equal(second.getItem('workspace'), null)
  } finally {
    if (oldDescriptor) Object.defineProperty(globalThis, 'sessionStorage', oldDescriptor)
    else delete globalThis.sessionStorage
    globalThis.location = oldLocation
  }
})

test('account finalization is document-local, URL-stripped, and distinct from invitation consent', () => {
  const paths = []
  const history = { replaceState: (_state, _title, path) => paths.push(path) }
  const entry = consumeSharedBrowserEntry({ hash: '#account-finalize=flow-123', pathname: '/shell/shared', search: '' }, history)
  assert.deepEqual(entry, { kind: 'account', value: 'flow-123' })
  assert.deepEqual(paths, ['/shell/shared'])
  for (const hash of ['#account-finalize', '#invite=x&account-finalize=y', '#account-finalize=x&account-finalize=y']) {
    assert.deepEqual(consumeSharedBrowserEntry({ hash, pathname: '/shell/shared', search: '' }, history), { kind: 'invalid' })
  }
})

test('a leave proof is its own admission, never a sign-in', () => {
  const history = { replaceState: () => {} }
  assert.deepEqual(
    consumeSharedBrowserEntry({ hash: '#account-leave=flow-9', pathname: '/shell/shared', search: '' }, history),
    { kind: 'leave', value: 'flow-9' },
  )
  for (const hash of ['#account-leave', '#account-leave=x&account-finalize=y', '#account-leave=x&account-leave=y']) {
    assert.deepEqual(consumeSharedBrowserEntry({ hash, pathname: '/shell/shared', search: '' }, history), { kind: 'invalid' })
  }
})

test('workspace storage is owner origin storage, or one grant store for a guest', () => {
  const oldLocation = globalThis.location
  const ownerLocal = { getItem: () => 'owner' }
  const values = new Map()
  globalThis.localStorage = ownerLocal
  globalThis.sessionStorage = {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
  try {
    globalThis.location = { pathname: '/shell/' }
    setActiveSharedBrowserGrantId('grant-A')
    assert.equal(isOwnerWorkspace(), true)
    assert.equal(localStore(), ownerLocal)
    assert.equal(ownerStore(), ownerLocal)
    assert.equal(sessionStore(), globalThis.sessionStorage)

    globalThis.location = { pathname: '/shell/shared' }
    assert.equal(isOwnerWorkspace(), false)
    assert.equal(ownerStore(), null)
    const guest = localStore()
    assert.equal(sessionStore(), guest)
    guest.setItem('k', 'v')
    assert.deepEqual([...values.keys()], ['mobius:shared-browser:grant-A:k'])
    setActiveSharedBrowserGrantId(null)
    assert.equal(localStore(), null)
    assert.equal(sessionStore(), null)
  } finally {
    setActiveSharedBrowserGrantId(null)
    globalThis.location = oldLocation
  }
})
