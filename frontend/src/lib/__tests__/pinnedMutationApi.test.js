import test from 'node:test'
import assert from 'node:assert/strict'

import {
  api,
  PINNED_MUTATION_TIMEOUT_MS,
} from '../../api/client.js'
import { SHELL_DATA_CACHE } from '../../sw-cache-policy.js'

function neverSettlingFetch(_url, { signal }) {
  return new Promise((_resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason)
      return
    }
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
}

test('a timed-out pin does not prevent the next owner intent', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = neverSettlingFetch
  assert.equal(PINNED_MUTATION_TIMEOUT_MS, 15_000)

  await assert.rejects(
    api.chats.setPinned('chat-1', true, { timeoutMs: 5 }),
    error => error?.name === 'TimeoutError',
  )

  globalThis.fetch = async () => new Response(JSON.stringify({
    ok: true,
    pinned_at: null,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  assert.equal((await api.chats.setPinned('chat-1', false)).pinned_at, null)
})

test('the mutation deadline covers a response body that never finishes', async (t) => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = async (_url, { signal }) => new Response(
    new ReadableStream({
      start(controller) {
        signal.addEventListener('abort', () => {
          controller.error(signal.reason)
        }, { once: true })
      },
    }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  )

  await assert.rejects(
    api.chats.setPinned('chat-1', true, { timeoutMs: 5 }),
    error => error?.name === 'TimeoutError',
  )
})

test('a reorder retires both offline shell-list snapshots', async (t) => {
  const originalFetch = globalThis.fetch
  const hadCaches = Object.hasOwn(globalThis, 'caches')
  const originalCaches = globalThis.caches
  const hadLocation = Object.hasOwn(globalThis, 'location')
  const originalLocation = globalThis.location
  t.after(() => {
    globalThis.fetch = originalFetch
    if (hadCaches) globalThis.caches = originalCaches
    else delete globalThis.caches
    if (hadLocation) globalThis.location = originalLocation
    else delete globalThis.location
  })

  const deleted = []
  globalThis.caches = {
    async open(name) {
      assert.equal(name, SHELL_DATA_CACHE)
      return {
        async delete(url) {
          deleted.push(url)
          return true
        },
      }
    },
  }
  globalThis.location = { origin: 'https://mobius.test' }
  globalThis.fetch = async () => new Response(JSON.stringify({
    items: [
      { kind: 'chat', id: 'chat-1', pinned_at: '2026-09-12T12:00:00' },
      { kind: 'app', id: '2', pinned_at: '2026-09-12T12:00:00.000001' },
    ],
  }), { status: 200, headers: { 'Content-Type': 'application/json' } })

  await api.chats.reorderPinned([
    { kind: 'chat', id: 'chat-1' },
    { kind: 'app', id: '2' },
  ])

  assert.deepEqual(deleted.sort(), [
    'https://mobius.test/api/apps/',
    'https://mobius.test/api/chats',
  ])
})
