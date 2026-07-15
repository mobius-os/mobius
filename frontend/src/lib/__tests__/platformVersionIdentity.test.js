import test from 'node:test'
import assert from 'node:assert/strict'

import { platformVersionIdentity } from '../platformVersionIdentity.js'

test('the recorded origin commit is the primary synced identity', () => {
  assert.deepEqual(platformVersionIdentity(
    { recorded_upstream_sha: '80754075ff2a9a08364c237f0cb708c8a25e4164' },
    { served_sha: '596f04083a35721bbd6ad384623a03cb9dc50324' },
  ), {
    primarySha: '8075407',
    synced: true,
    localSha: '596f040',
  })
})

test('served or image identity remains a fallback for untracked builds', () => {
  assert.deepEqual(platformVersionIdentity({}, { served_sha: 'abc123456' }), {
    primarySha: 'abc1234', synced: false, localSha: null,
  })
  assert.deepEqual(platformVersionIdentity({}, { sha: 'def567890' }), {
    primarySha: 'def5678', synced: false, localSha: null,
  })
})
