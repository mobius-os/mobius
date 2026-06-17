import assert from 'node:assert/strict'
import { test } from 'node:test'

import { appVersionKey } from '../appVersion.js'

test('appVersionKey preserves sub-second updated_at precision', () => {
  const a = '2026-06-04T12:00:00.123456Z'
  const b = '2026-06-04T12:00:00.987654Z'

  assert.notEqual(appVersionKey(a), appVersionKey(b))
  assert.equal(appVersionKey(a), a)
})

test('appVersionKey has a stable empty fallback', () => {
  assert.equal(appVersionKey(null), '0')
  assert.equal(appVersionKey(''), '0')
})

import { moduleVersionKey } from '../appVersion.js'

test('moduleVersionKey strips the 16-hex frameRev suffix', () => {
  assert.equal(moduleVersionKey('2026-06-13 22:56:42.548785-a1b2c3d4e5f67890'), '2026-06-13 22:56:42.548785')
  assert.equal(moduleVersionKey('0-0123456789abcdef'), '0')
})

test('moduleVersionKey preserves a version with no frameRev', () => {
  assert.equal(moduleVersionKey('2026-06-13 22:56:42.548785'), '2026-06-13 22:56:42.548785')
  assert.equal(moduleVersionKey('0'), '0')
  assert.equal(moduleVersionKey(''), '')
})

test('moduleVersionKey strips ONLY an exact trailing -<16 lowercase hex>, keeping other hyphens', () => {
  // The suffix removed is exactly /-[0-9a-f]{16}$/ -- a 16-hex tail IS stripped
  // (even off a semver-looking string, line below), but any non-16-hex
  // hyphenated tail is preserved.
  assert.equal(moduleVersionKey('1.2.0-beta.1-a1b2c3d4e5f67890'), '1.2.0-beta.1')
  assert.equal(moduleVersionKey('1.2.0-beta.1'), '1.2.0-beta.1')
  assert.equal(moduleVersionKey('foo-abcdef012345678'), 'foo-abcdef012345678')   // 15 hex
  assert.equal(moduleVersionKey('foo-abcdef0123456789a'), 'foo-abcdef0123456789a') // 17 hex
})
