import assert from 'node:assert/strict'
import { test } from 'node:test'

import { appFrameVersion } from '../appVersion.js'

test('appFrameVersion follows frame_version, not updated_at', () => {
  const app = { frame_version: '0123456789abcdef0123', updated_at: '2026-06-04T12:00:00.1Z' }

  assert.equal(appFrameVersion(app), '0123456789abcdef0123')
  assert.equal(
    appFrameVersion({ ...app, updated_at: '2026-06-04T12:00:09.9Z' }),
    appFrameVersion(app),
  )
  assert.equal(moduleVersionKey(`${appFrameVersion(app)}-a1b2c3d4e5f67890`), app.frame_version)
})

test('appFrameVersion has a stable missing-row fallback', () => {
  assert.equal(appFrameVersion(undefined), '0')
  assert.equal(appFrameVersion(null), '0')
})

test('appFrameVersion preserves updates while the old backend awaits restart', () => {
  const before = { updated_at: '2026-06-04T12:00:00Z' }
  const after = { updated_at: '2026-06-04T12:00:09Z' }
  assert.equal(appFrameVersion(before), before.updated_at)
  assert.equal(appFrameVersion(after), after.updated_at)
  assert.notEqual(appFrameVersion(before), appFrameVersion(after))
  assert.equal(appFrameVersion({ ...after, frame_version: ' ' }), after.updated_at)
  assert.equal(moduleVersionKey(`${appFrameVersion(after)}-a1b2c3d4e5f67890`), after.updated_at)
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
