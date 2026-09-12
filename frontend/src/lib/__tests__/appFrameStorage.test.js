import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  cacheAppToken, clearAppFrameStorage, clearCachedAppToken, isSafeVirtualStorageKey,
  migrateLegacyAppFrameStorage, readAppFrameStorage, readCachedAppToken,
  removeAppFrameStorage,
  setAppFrameStorage, _storagePrefixes,
} from '../appFrameStorage.js'

class MemoryStorage {
  constructor(entries = {}) { this.values = new Map(Object.entries(entries)) }
  get length() { return this.values.size }
  key(index) { return [...this.values.keys()][index] ?? null }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null }
  setItem(key, value) { this.values.set(String(key), String(value)) }
  removeItem(key) { this.values.delete(String(key)) }
}

function jwt(claims) {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'none' })}.${encode(claims)}.signature`
}

test('one-time migration never claims storage from a numeric key heuristic', () => {
  const storage = new MemoryStorage({
    token: 'owner-secret',
    'mobius:app-token:7': 'cached-secret',
    moebius_active_chat: 'private-chat-id',
    moebius_active_view: 'canvas',
    'mobius-app-lru': '[7,8]',
    'news:8:cache': 'sibling preference',
    'news:7:cache': 'safe preference',
    highscores: '[999]',
    'tn-split-ratio-v2': '0.4',
    'mobius-theme': 'dark',
  })
  assert.deepEqual(readAppFrameStorage(7, storage, 'news'), {})
  assert.equal(
    storage.getItem(`${_storagePrefixes.LEGACY_MIGRATION_PREFIX}7`),
    'done',
  )
  assert.equal(storage.getItem('news:7:cache'), 'safe preference')
})

test('catalog-only CubeRun and Tandem preferences survive the cutover', () => {
  const cube = new MemoryStorage({ highscores: '[999]', musicEnabled: 'false' })
  assert.equal(migrateLegacyAppFrameStorage(4, 'cuberun', cube), true)
  assert.deepEqual(readAppFrameStorage(4, cube, 'cuberun'), {
    highscores: '[999]', musicEnabled: 'false',
  })
  const tandem = new MemoryStorage({ 'tn-split-ratio-v2': '0.4' })
  assert.equal(migrateLegacyAppFrameStorage(5, 'tandem', tandem), true)
  assert.deepEqual(readAppFrameStorage(5, tandem, 'tandem'), {
    'tn-split-ratio-v2': '0.4',
  })
})

test('migration is idempotent and never overwrites a current app value', () => {
  const storage = new MemoryStorage({
    highscores: 'legacy',
    'mobius:app-frame-storage:7:highscores': 'current',
  })
  assert.deepEqual(readAppFrameStorage(7, storage, 'cuberun'), {
    highscores: 'current',
  })
  storage.setItem('highscores', 'changed after proof')
  assert.deepEqual(readAppFrameStorage(7, storage, 'cuberun'), {
    highscores: 'current',
  })
})

test('a failed copy writes no proof and retries on the next mount', () => {
  class FailingStorage extends MemoryStorage {
    constructor(entries) { super(entries); this.failCopy = true }
    setItem(key, value) {
      if (this.failCopy && String(key).startsWith('mobius:app-frame-storage:7:')) {
        this.failCopy = false
        throw new Error('quota')
      }
      super.setItem(key, value)
    }
  }
  const storage = new FailingStorage({ highscores: 'legacy' })
  assert.deepEqual(readAppFrameStorage(7, storage, 'cuberun'), {})
  assert.equal(storage.getItem(`${_storagePrefixes.LEGACY_MIGRATION_PREFIX}7`), null)
  assert.deepEqual(readAppFrameStorage(7, storage, 'cuberun'), {
    highscores: 'legacy',
  })
  assert.equal(storage.getItem(`${_storagePrefixes.LEGACY_MIGRATION_PREFIX}7`), 'done')
})

test('app writes are isolated from shell and sibling storage', () => {
  const storage = new MemoryStorage({ 'shared-legacy': 'old' })
  assert.equal(setAppFrameStorage(7, 'theme', 'dark', storage), true)
  assert.equal(storage.getItem('theme'), null)
  assert.equal(readAppFrameStorage(7, storage).theme, 'dark')
  assert.equal(readAppFrameStorage(8, storage).theme, undefined)
  removeAppFrameStorage(7, 'theme', storage)
  assert.equal(readAppFrameStorage(7, storage).theme, undefined)
  setAppFrameStorage(7, 'one', '1', storage)
  setAppFrameStorage(7, 'two', '2', storage)
  clearAppFrameStorage(7, storage)
  assert.equal(readAppFrameStorage(7, storage).one, undefined)
  assert.equal(storage.getItem('shared-legacy'), 'old')
})

test('only the two setup coordination keys are shared across app frames', () => {
  const storage = new MemoryStorage()
  setAppFrameStorage(7, 'mobius:setup-complete:v1', '{"news":true}', storage)
  assert.equal(
    readAppFrameStorage(8, storage)['mobius:setup-complete:v1'],
    '{"news":true}',
  )
  setAppFrameStorage(7, 'private-pref', '7-only', storage)
  assert.equal(readAppFrameStorage(8, storage)['private-pref'], undefined)
})

test('sensitive and malformed virtual keys are rejected', () => {
  assert.equal(isSafeVirtualStorageKey('token'), false)
  assert.equal(isSafeVirtualStorageKey('refresh_token'), false)
  assert.equal(isSafeVirtualStorageKey('ordinary-pref'), true)
})

test('cached app token must match the exact app id and may be retained for offline boot', () => {
  const now = Date.parse('2026-07-13T00:00:00Z')
  const storage = new MemoryStorage()
  const valid = jwt({ scope: 'app', app_id: 7, exp: now / 1000 + 3600 })
  assert.equal(cacheAppToken(7, valid, storage), true)
  assert.equal(readCachedAppToken(7, storage, now), valid)
  assert.equal(readCachedAppToken(8, storage, now), undefined)

  const expired = jwt({ scope: 'app', app_id: 7, exp: now / 1000 - 1 })
  assert.equal(cacheAppToken(7, expired, storage), true)
  assert.equal(readCachedAppToken(7, storage, now), undefined)
  assert.equal(
    readCachedAppToken(7, storage, now, { allowExpired: true }),
    expired,
  )
  clearCachedAppToken(7, storage)
  assert.equal(readCachedAppToken(7, storage, now, { allowExpired: true }), undefined)
  assert.equal(cacheAppToken(8, valid, storage), false)
})
