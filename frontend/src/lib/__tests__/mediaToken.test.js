import { after, before, test } from 'node:test'
import assert from 'node:assert/strict'

import {
  beginEphemeralAuth,
  clearEphemeralAuthSession,
  setEphemeralAuthSession,
} from '../../api/client.js'
import { clearMediaTokenCache, mediaTokenParam } from '../../api/mediaToken.js'

const previousFetch = globalThis.fetch

before(() => {
  beginEphemeralAuth()
})

after(() => {
  clearEphemeralAuthSession()
  clearMediaTokenCache()
  globalThis.fetch = previousFetch
})

test('embedded media cache rotates with the ephemeral chat session', async () => {
  let mintCount = 0
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    async json() { return { token: `media-${++mintCount}` } },
  })

  setEphemeralAuthSession('session-old', 'instance-1')
  assert.equal(await mediaTokenParam('chat-1'), '?token=media-1')
  assert.equal(await mediaTokenParam('chat-1'), '?token=media-1')
  assert.equal(mintCount, 1, 'same embedded session should reuse its media token')

  setEphemeralAuthSession('session-new', 'instance-1')
  assert.equal(await mediaTokenParam('chat-1'), '?token=media-2')
  assert.equal(mintCount, 2, 'successful session replacement must mint new media authority')

  clearEphemeralAuthSession()
  assert.equal(await mediaTokenParam('chat-1'), '?token=media-3')
  assert.equal(mintCount, 3, 'clearing the session must invalidate its cached media token')
})
