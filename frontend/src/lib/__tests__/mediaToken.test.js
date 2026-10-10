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
  openMediaLink,
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

async function settle() {
  for (let i = 0; i < 20; i += 1) await new Promise(resolve => setTimeout(resolve, 0))
}

async function withGlobals(overrides, fn) {
  const previous = Object.fromEntries(Object.keys(overrides).map(key => [key, globalThis[key]]))
  Object.assign(globalThis, overrides)
  try {
    return await fn()
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
}

function clickEvent(extra = {}) {
  const event = { button: 0, prevented: false, ...extra }
  event.preventDefault = () => { event.prevented = true }
  return event
}

test('file links open with a token minted at click time, not render time', async () => {
  clearMediaTokenCache()
  setEphemeralAuthSession('session-click', 'instance-1')
  let mintCount = 0
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    async json() { return { token: `media-${++mintCount}` } },
  })
  const originalNow = Date.now
  let now = 5_000_000
  Date.now = () => now
  const opened = []
  const tab = { opener: 'shell', location: { replace(url) { tab.url = url } } }
  try {
    // The link renders with the first token; the owner clicks 16 minutes later.
    assert.equal(await mediaTokenParam('chat-click'), '?token=media-1')
    now += 16 * 60 * 1000
    await withGlobals({ window: { open: (...args) => { opened.push(args); return tab } } }, async () => {
      const event = clickEvent()
      const path = '/api/chats/chat-click/generated-files/trailer.mp4'
      openMediaLink(event, 'chat-click', path, { query: '&preview=true' })
      assert.equal(event.prevented, true)
      assert.deepEqual(opened, [['', '_blank']], 'the tab opens inside the click so popup blockers allow it')
      await settle()
      assert.equal(tab.url, `${path}?token=media-2&preview=true`)
      assert.equal(tab.opener, null)
    })
  } finally {
    Date.now = originalNow
  }
})

test('modified clicks keep the browser default', async () => {
  const opened = []
  await withGlobals({ window: { open: (...args) => { opened.push(args); return {} } } }, async () => {
    for (const extra of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
      const event = clickEvent(extra)
      openMediaLink(event, 'chat-mod', '/api/chats/chat-mod/uploads/a.pdf')
      assert.equal(event.prevented, false)
    }
  })
  assert.equal(opened.length, 0)
})

test('a blocked popup falls back to the link itself', async () => {
  await withGlobals({ window: { open: () => null } }, async () => {
    const event = clickEvent()
    openMediaLink(event, 'chat-blocked', '/api/chats/chat-blocked/uploads/a.pdf')
    assert.equal(event.prevented, false)
  })
})

test('downloads also use a token that is valid at click time', async () => {
  clearMediaTokenCache()
  setEphemeralAuthSession('session-download', 'instance-1')
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    async json() { return { token: 'media-download' } },
  })
  const created = []
  const document = {
    createElement() {
      const element = { click() { element.clicked = true }, remove() { element.removed = true } }
      created.push(element)
      return element
    },
    body: { appendChild(element) { element.attached = true } },
  }
  const window = { open() { throw new Error('a download must not open a tab') } }
  await withGlobals({ document, window }, async () => {
    const event = clickEvent()
    const path = '/api/chats/chat-download/generated-files/report.zip'
    openMediaLink(event, 'chat-download', path, { download: 'report.zip' })
    assert.equal(event.prevented, true)
    await settle()
    assert.equal(created.length, 1)
    assert.equal(created[0].href, `${path}?token=media-download`)
    assert.equal(created[0].download, 'report.zip')
    assert.deepEqual([created[0].attached, created[0].clicked, created[0].removed], [true, true, true])
  })
})
