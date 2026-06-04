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
