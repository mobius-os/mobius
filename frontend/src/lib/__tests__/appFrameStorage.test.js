import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  cacheAppToken, clearAppFrameStorage, clearCachedAppToken, isSafeVirtualStorageKey,
  isLegacyFrameStorageKey, readAppFrameStorage, readCachedAppToken,
  removeAppFrameStorage,
  setAppFrameStorage,
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

test('frame snapshot never exposes owner auth or internal token caches', () => {
  const storage = new MemoryStorage({
    token: 'owner-secret',
    'mobius:app-token:7': 'cached-secret',
    moebius_active_chat: 'private-chat-id',
    moebius_active_view: 'canvas',
    'mobius-app-lru': '[7,8]',
    'news:8:cache': 'sibling preference',
    'news:7:cache': 'safe preference',
  })
  assert.deepEqual(readAppFrameStorage(7, storage), {
    'news:7:cache': 'safe preference',
  })
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

test('legacy unscoped preferences are limited to their catalog app', () => {
  assert.equal(isLegacyFrameStorageKey(7, 'cuberun', 'highscores'), true)
  assert.equal(isLegacyFrameStorageKey(7, 'news', 'highscores'), false)
  assert.equal(isLegacyFrameStorageKey(7, 'tandem', 'tn-split-ratio-v2'), true)
  assert.equal(isLegacyFrameStorageKey(7, 'news', 'moebius_active_chat'), false)
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
