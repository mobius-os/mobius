import test from 'node:test'
import assert from 'node:assert/strict'
import {
  appUpdateNoticeIdentity,
  dismissAppUpdate,
  isAppUpdateDismissed,
} from '../appUpdateNotice.js'

const digestA = 'a'.repeat(64)
const digestB = 'b'.repeat(64)

function memoryStorage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  }
}

test('only a known content update with a full candidate digest has an identity', () => {
  assert.equal(appUpdateNoticeIdentity({
    update_available: true,
    candidate_source_digest: digestA,
  }), digestA)
  assert.equal(appUpdateNoticeIdentity({
    update_available: false,
    candidate_source_digest: digestA,
  }), null)
  assert.equal(appUpdateNoticeIdentity({
    update_available: null,
    upstream_version: '9.0.0',
  }), null)
  assert.equal(appUpdateNoticeIdentity({
    update_available: true,
    upstream_version: '9.0.0',
  }), null)
})

test('dismissing one candidate hides only that app and candidate', () => {
  const storage = memoryStorage()
  assert.equal(isAppUpdateDismissed(12, digestA, storage), false)
  assert.equal(dismissAppUpdate(12, digestA, storage), true)
  assert.equal(isAppUpdateDismissed(12, digestA, storage), true)
  assert.equal(isAppUpdateDismissed(13, digestA, storage), false)
  assert.equal(isAppUpdateDismissed(12, digestB, storage), false)
  assert.equal(dismissAppUpdate(12, digestB, storage), true)
  assert.equal(isAppUpdateDismissed(12, digestA, storage), false)
  assert.equal(isAppUpdateDismissed(12, digestB, storage), true)
})

test('unavailable browser storage never blocks the app or claims a dismissal', () => {
  const unavailable = {
    getItem() { throw new Error('storage denied') },
    setItem() { throw new Error('storage denied') },
  }
  assert.equal(isAppUpdateDismissed(12, digestA, unavailable), false)
  assert.equal(dismissAppUpdate(12, digestA, unavailable), false)
})
