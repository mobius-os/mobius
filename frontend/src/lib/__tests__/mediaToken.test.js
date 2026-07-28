import { after, before, test } from 'node:test'
import assert from 'node:assert/strict'

import {
  beginEphemeralAuth,
  clearEphemeralAuthSession,
  setEphemeralAuthSession,
} from '../../api/client.js'
import {
  clearMediaTokenCache,
  mediaTokenParam,
} from '../../api/mediaToken.js'

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
  clearMediaTokenCache()
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

test('expired media tokens are removed and minted again', async () => {
  clearMediaTokenCache()
  setEphemeralAuthSession('session-expiry', 'instance-1')
  let mintCount = 0
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    async json() { return { token: `media-${++mintCount}` } },
  })
  const originalNow = Date.now
  let now = 1_000_000
  Date.now = () => now
  try {
    assert.equal(await mediaTokenParam('chat-expiry'), '?token=media-1')
    now += 10 * 60 * 1000
    assert.equal(await mediaTokenParam('chat-expiry'), '?token=media-2')
    assert.equal(mintCount, 2)
  } finally {
    Date.now = originalNow
  }
})

test('current media tokens remain reusable across a large working set', async () => {
  clearMediaTokenCache()
  setEphemeralAuthSession('session-churn', 'instance-1')
  let mintCount = 0
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    async json() { return { token: `media-${++mintCount}` } },
  })

  const chatCount = 64
  for (let index = 0; index < chatCount; index += 1) {
    await mediaTokenParam(`chat-${index}`)
  }
  await mediaTokenParam('chat-0')

  assert.equal(
    mintCount,
    chatCount,
    'a still-current token should not be evicted because other chats were used',
  )
})
