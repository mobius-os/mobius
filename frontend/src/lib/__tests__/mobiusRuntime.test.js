/**
 * Unit tests for the PURE read-your-writes / LWW logic in the mini-app
 * runtime (frontend/public/mobius-runtime.js → overlayPending).
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/mobiusRuntime.test.js
 *
 * The rest of the runtime (IndexedDB outbox + cache store + subscribe) needs a
 * browser and is covered by the persistent-profile Playwright e2e. overlayPending
 * is the single source of truth for "what value should the caller see right now"
 * given the pending outbox + the server/cache fallback — so it gets a focused,
 * deterministic test here, the same way appToken.js extracts its decision logic.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  appChatMetadataBody,
  makeNav,
  overlayPending,
} from '../../../public/mobius-runtime.js'

const SERVER = { value: 5 }       // a fallback (server/cache mirror) value
const QUEUED = { value: 9 }

const put = (path, data) => ({ method: 'PUT', path, data })
const del = (path) => ({ method: 'DELETE', path })

test('no pending op → fallback stands (the cached/server value)', () => {
  assert.deepEqual(overlayPending([], 'hi.json', SERVER), SERVER)
  assert.deepEqual(overlayPending([put('other.json', QUEUED)], 'hi.json', SERVER), SERVER)
})

test('a pending PUT for the path wins over the fallback (read-your-writes)', () => {
  assert.deepEqual(overlayPending([put('hi.json', QUEUED)], 'hi.json', SERVER), QUEUED)
})

test('a pending DELETE for the path resolves to null', () => {
  assert.equal(overlayPending([del('hi.json')], 'hi.json', SERVER), null)
})

test('the NEWEST queued op for a path wins (FIFO order, last entry)', () => {
  // The outbox coalesces to one op per path, but be robust if several survive:
  // the last (newest by seq) must win — that is LWW.
  const ops = [put('hi.json', { value: 1 }), put('hi.json', { value: 2 }), put('hi.json', QUEUED)]
  assert.deepEqual(overlayPending(ops, 'hi.json', SERVER), QUEUED)
  // A later DELETE supersedes an earlier PUT.
  assert.equal(overlayPending([put('hi.json', QUEUED), del('hi.json')], 'hi.json', SERVER), null)
  // A later PUT supersedes an earlier DELETE (re-created offline).
  assert.deepEqual(overlayPending([del('hi.json'), put('hi.json', QUEUED)], 'hi.json', SERVER), QUEUED)
})

test('fallback null with no pending → null (never-cached / known-absent)', () => {
  assert.equal(overlayPending([], 'hi.json', null), null)
})

test('pending op only affects its own path', () => {
  const ops = [put('a.json', QUEUED), del('b.json')]
  assert.deepEqual(overlayPending(ops, 'a.json', SERVER), QUEUED)
  assert.equal(overlayPending(ops, 'b.json', SERVER), null)
  assert.deepEqual(overlayPending(ops, 'c.json', SERVER), SERVER)
})

test('app chat metadata body preserves explicit clears', () => {
  assert.deepEqual(appChatMetadataBody({
    systemPrompt: '',
    model: null,
    provider: 'codex',
  }), {
    system_prompt: '',
    model: '',
    provider: 'codex',
  })
})

test('app chat metadata body can omit provider for existing-chat updates', () => {
  assert.deepEqual(appChatMetadataBody({
    systemPrompt: 'You are inside Notes.',
    model: '',
    provider: 'codex',
  }, { includeProvider: false }), {
    system_prompt: 'You are inside Notes.',
    model: '',
  })
})

test('app chat metadata body forwards scoped chat fields', () => {
  assert.deepEqual(appChatMetadataBody({
    scope: ' workout-session:session-123 ',
    scopeLabel: ' Workout Jul 11 ',
  }), {
    scope: 'workout-session:session-123',
    scope_label: 'Workout Jul 11',
  })
})

async function withFakeWindow(fn) {
  const previousWindow = globalThis.window
  const listeners = new Set()
  const parent = {
    messages: [],
    postMessage(data, origin) {
      this.messages.push({ data, origin })
    },
  }
  const fakeWindow = {
    location: { origin: 'https://mobius.test' },
    parent,
    addEventListener(type, cb) {
      if (type === 'message') listeners.add(cb)
    },
    removeEventListener(type, cb) {
      if (type === 'message') listeners.delete(cb)
    },
    emit(data, { origin = 'https://mobius.test', source = parent } = {}) {
      for (const cb of [...listeners]) cb({ data, origin, source })
    },
  }
  globalThis.window = fakeWindow
  try {
    await fn({ window: fakeWindow, parent })
  } finally {
    globalThis.window = previousWindow
  }
}

test('nav helper waits for ack before owning a back entry', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const nav = makeNav()
    let backed = false
    const handle = nav.open('detail', () => { backed = true })
    const push = parent.messages.at(-1).data
    assert.equal(push.type, 'moebius:nav-push')
    assert.equal(push.label, 'detail')

    window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
    assert.equal(await handle.ready, true)

    window.emit({ type: 'moebius:nav-back' })
    assert.equal(backed, true)
    assert.equal(parent.messages.some((msg) => msg.data.type === 'moebius:nav-pop'), false)
  })
})

test('nav helper ignores same-origin messages from non-parent frames', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const nav = makeNav()
    const handle = nav.open('detail')
    const push = parent.messages.at(-1).data

    window.emit(
      { type: 'moebius:nav-push-ack', requestId: push.requestId },
      { source: { postMessage() {} } },
    )
    handle.close()
    window.emit({ type: 'moebius:nav-push-rejected', requestId: push.requestId })
    assert.equal(await handle.ready, false)
    assert.equal(parent.messages.some((msg) => msg.data.type === 'moebius:nav-pop'), false)
  })
})

test('nav helper handles rejected pushes without owning or popping', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const nav = makeNav()
    const handle = nav.open('detail')
    const push = parent.messages.at(-1).data

    window.emit({ type: 'moebius:nav-push-rejected', requestId: push.requestId })
    assert.equal(await handle.ready, false)
    handle.close()
    assert.equal(parent.messages.some((msg) => msg.data.type === 'moebius:nav-pop'), false)
  })
})

test('nav helper auto-pops a late ack after local close', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const nav = makeNav()
    const handle = nav.open('detail')
    const push = parent.messages.at(-1).data

    handle.close()
    assert.equal(await handle.ready, false)
    window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
    assert.equal(parent.messages.at(-1).data.type, 'moebius:nav-pop')
  })
})
