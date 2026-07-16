import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  beginEphemeralAuth,
  clearEphemeralAuthSession,
  getAuthHeaders,
  getToken,
  setEphemeralAuthSession,
} from '../../api/client.js'

test('embedded chat exposes only its memory-only scoped session', () => {
  let storageReads = 0
  globalThis.localStorage = {
    getItem() { storageReads += 1; return 'owner-token-must-stay-hidden' },
  }
  beginEphemeralAuth()
  assert.equal(getToken(), null)
  assert.equal(storageReads, 0)

  setEphemeralAuthSession('chat-embed-session', 'embed-instance')
  assert.equal(getToken(), 'chat-embed-session')
  assert.deepEqual(getAuthHeaders(), {
    Authorization: 'Bearer chat-embed-session',
    'X-Mobius-Embed-Instance': 'embed-instance',
  })
  assert.equal(storageReads, 0)

  clearEphemeralAuthSession()
  assert.equal(getToken(), null)
  assert.equal(storageReads, 0)
})
