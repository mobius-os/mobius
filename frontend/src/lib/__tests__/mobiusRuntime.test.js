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
  makeChat,
  makeCapabilities,
  makeNav,
  makeSignal,
  overlayPending,
  sanitizeEmbedGuidance,
} from '../../../public/mobius-runtime.js'

const SERVER = { value: 5 }       // a fallback (server/cache mirror) value
const QUEUED = { value: 9 }

const put = (path, data) => ({ method: 'PUT', path, data })
const del = (path) => ({ method: 'DELETE', path })

test('runtime normalizes app guidance at the same boundary used by setGuidance', () => {
  assert.equal(sanitizeEmbedGuidance('  Edit the selected file.  '), 'Edit the selected file.')
  assert.equal(sanitizeEmbedGuidance('   '), null)
  assert.equal(sanitizeEmbedGuidance(42), null)
  assert.equal(sanitizeEmbedGuidance('x'.repeat(500)).length, 300)
})

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

test('app chat metadata body exposes owner visibility only on create', () => {
  assert.deepEqual(appChatMetadataBody({
    ownerVisible: true,
  }, { includeOwnerVisible: true }), {
    owner_visible: true,
  })
  assert.deepEqual(appChatMetadataBody({ ownerVisible: true }), {})
})

test('chat.start creates one owner-visible app chat and submits its first turn', async () => {
  const previousFetch = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, init })
    if (url === '/api/app-chats') {
      return new Response(JSON.stringify({ id: 'chat-started' }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url === '/api/chats/chat-started/messages') {
      return new Response(JSON.stringify({ status: 'started' }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return new Response(null, { status: 404 })
  }
  try {
    const chat = makeChat({
      appId: 80,
      getToken: async () => 'app-token',
      storage: null,
    })
    const result = await chat.start({
      title: 'Address Contribute follow-ups',
      draft: 'Address every active follow-up.',
    })

    assert.equal(result.chatId, 'chat-started')
    assert.equal(calls.length, 2)
    assert.deepEqual(JSON.parse(calls[0].init.body), {
      title: 'Address Contribute follow-ups',
      owner_visible: true,
    })
    const sent = JSON.parse(calls[1].init.body)
    assert.equal(sent.content, 'Address every active follow-up.')
    assert.equal(typeof sent.cid, 'string')
    assert.equal(typeof sent.timezone, 'string')
  } finally {
    globalThis.fetch = previousFetch
  }
})

test('chat.start rejects an empty first turn before creating a chat', async () => {
  const chat = makeChat({
    appId: 80,
    getToken: async () => 'app-token',
    storage: null,
  })
  await assert.rejects(
    chat.start({ draft: '   ' }),
    /opts\.draft must not be empty/,
  )
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
    assert.deepEqual(await handle.outcome, { status: 'owned' })
    assert.equal(await handle.ready, true)

    window.emit({ type: 'moebius:nav-back' })
    assert.equal(backed, true)
    assert.equal(parent.messages.some((msg) => msg.data.type === 'moebius:nav-pop'), false)
  })
})

test('reversible nav restores the same app view on Forward and can unwind again', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const events = []
    const nav = makeNav()
    const handle = nav.open('report', {
      onBack: () => events.push('back'),
      onForward: () => events.push('forward'),
    })
    const push = parent.messages.at(-1).data
    assert.equal(push.reversible, true)
    window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
    assert.deepEqual(await handle.outcome, { status: 'owned' })

    window.emit({ type: 'moebius:nav-back', requestId: push.requestId })
    assert.deepEqual(events, ['back'])
    assert.equal(parent.messages.filter((msg) => msg.data.type === 'moebius:nav-pop').length, 0)

    window.emit({ type: 'moebius:nav-forward', requestId: push.requestId })
    assert.deepEqual(events, ['back', 'forward'])
    assert.equal(
      parent.messages.filter((msg) => msg.data.type === 'moebius:nav-forward-ack').length,
      1,
    )
    window.emit({ type: 'moebius:nav-back', requestId: push.requestId })
    assert.deepEqual(events, ['back', 'forward', 'back'])
  })
})

test('a reversible in-app close keeps Forward restoration but emits one pop', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    let forwards = 0
    const handle = makeNav().open('report', {
      onBack() {},
      onForward() { forwards += 1 },
    })
    const push = parent.messages.at(-1).data
    window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
    await handle.outcome

    handle.close()
    assert.equal(parent.messages.filter((msg) => msg.data.type === 'moebius:nav-pop').length, 1)
    window.emit({ type: 'moebius:nav-forward', requestId: push.requestId })
    assert.equal(forwards, 1)
    assert.equal(parent.messages.at(-1).data.type, 'moebius:nav-forward-ack')
  })
})

test('a fresh runtime explicitly rejects Forward state it cannot reconstruct', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    makeNav()
    window.emit({ type: 'moebius:nav-forward', requestId: 'missing-entry' })
    assert.deepEqual(parent.messages.at(-1).data, {
      type: 'moebius:nav-forward-rejected',
      requestId: 'missing-entry',
    })
  })
})

test('Forward is rejected when onForward synchronously closes the restored view', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    let handle
    handle = makeNav().open('report', {
      onBack() {},
      onForward() { handle.close() },
    })
    const push = parent.messages.at(-1).data
    window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
    await handle.outcome
    window.emit({ type: 'moebius:nav-back', requestId: push.requestId })

    window.emit({ type: 'moebius:nav-forward', requestId: push.requestId })
    assert.equal(parent.messages.at(-1).data.type, 'moebius:nav-forward-rejected')
  })
})

test('microphone capability correlates shell capture and exposes PCM to the app', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const levels = []
    const capabilities = makeCapabilities({
      declarations: {
        'media.microphone.capture': {
          version: 1, limits: { max_duration_ms: 8000 },
        },
      },
    })
    const session = capabilities.open('media.microphone.capture', { maxDurationMs: 8000 })
    session.on('level', (value) => levels.push(value))
    const start = parent.messages.at(-1).data
    assert.equal(start.type, 'moebius:capability-open')
    assert.equal(start.capability, 'media.microphone.capture')
    assert.equal(start.input.maxDurationMs, 8000)

    window.emit({
      type: 'moebius:capability-ready', requestId: start.requestId,
      capability: start.capability, value: { sampleRate: 48000 },
    })
    assert.deepEqual(await session.ready, { sampleRate: 48000 })
    window.emit({
      type: 'moebius:capability-event', requestId: start.requestId,
      capability: start.capability, event: 'level', value: 0.6,
    })
    session.finish()
    assert.equal(parent.messages.at(-1).data.type, 'moebius:capability-control')
    assert.equal(parent.messages.at(-1).data.action, 'finish')

    const samples = new Float32Array([0.1, -0.2, 0.3])
    window.emit({
      type: 'moebius:capability-result', requestId: start.requestId,
      capability: start.capability, value: { sampleRate: 48000, samples },
    })
    const result = await session.result
    assert.equal(result.sampleRate, 48000)
    assert.deepEqual([...result.samples], [...samples])
    assert.deepEqual(levels, [0.6])
    capabilities._destroy()
  })
})

test('microphone capability can cancel while permission is still pending', async () => {
  await withFakeWindow(async ({ parent }) => {
    const capabilities = makeCapabilities({
      declarations: {
        'media.microphone.capture': {
          version: 1, limits: { max_duration_ms: 4000 },
        },
      },
    })
    const session = capabilities.open('media.microphone.capture', { maxDurationMs: 4000 })
    session.cancel()
    assert.equal(parent.messages.at(-1).data.type, 'moebius:capability-control')
    assert.equal(parent.messages.at(-1).data.action, 'cancel')
    await assert.rejects(session.ready, { name: 'AbortError' })
    await assert.rejects(session.result, { name: 'AbortError' })
    capabilities._destroy()
  })
})

test('capabilities reject direct top-level use instead of bypassing the host', async () => {
  const previousWindow = globalThis.window
  const topLevel = {
    location: { origin: 'https://mobius.test' },
    addEventListener() {},
    removeEventListener() {},
  }
  topLevel.parent = topLevel
  globalThis.window = topLevel
  try {
    const capabilities = makeCapabilities({
      declarations: {
        'media.microphone.capture': {
          version: 1, limits: { max_duration_ms: 2000 },
        },
      },
    })
    assert.equal(capabilities.available('media.microphone.capture'), false)
    assert.throws(
      () => capabilities.open('media.microphone.capture', { maxDurationMs: 2000 }),
      { code: 'unavailable' },
    )
    capabilities._destroy()
  } finally {
    globalThis.window = previousWindow
  }
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
    assert.deepEqual(await handle.outcome, { status: 'cancelled' })
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
    assert.deepEqual(await handle.outcome, { status: 'rejected' })
    assert.equal(await handle.ready, false)
    handle.close()
    assert.equal(parent.messages.some((msg) => msg.data.type === 'moebius:nav-pop'), false)
  })
})

test('nav helper close after ownership emits one pop and is idempotent', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const handle = makeNav().open('detail')
    const push = parent.messages.at(-1).data
    window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
    await handle.outcome

    handle.close()
    handle.close()
    assert.equal(parent.messages.filter((msg) => msg.data.type === 'moebius:nav-pop').length, 1)
  })
})

test('nav helper auto-pops a late ack after local close', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const nav = makeNav()
    const handle = nav.open('detail')
    const push = parent.messages.at(-1).data

    handle.close()
    assert.deepEqual(await handle.outcome, { status: 'cancelled' })
    assert.equal(await handle.ready, false)
    window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
    assert.equal(parent.messages.at(-1).data.type, 'moebius:nav-pop')
    assert.equal(parent.messages.filter((msg) => msg.data.type === 'moebius:nav-pop').length, 1)
    assert.deepEqual(await handle.outcome, { status: 'cancelled' })
  })
})

test('nav helper rejects direct top-level use instead of growing a second host', async () => {
  const previousWindow = globalThis.window
  const listeners = new Set()
  const standalone = {
    location: { origin: 'https://mobius.test' },
    addEventListener(type, cb) { if (type === 'message') listeners.add(cb) },
    removeEventListener(type, cb) { if (type === 'message') listeners.delete(cb) },
  }
  standalone.parent = standalone
  globalThis.window = standalone
  try {
    const handle = makeNav().open('detail')
    assert.deepEqual(await handle.outcome, { status: 'unavailable' })
    assert.equal(await handle.ready, false)
    assert.equal(listeners.size, 0)
  } finally {
    globalThis.window = previousWindow
  }
})

test('nav helper reports timeout and compensates a late acknowledgement', async () => {
  const previousSetTimeout = globalThis.setTimeout
  const previousClearTimeout = globalThis.clearTimeout
  const timers = []
  globalThis.setTimeout = (cb, ms) => {
    timers.push({ cb, ms })
    return timers.length
  }
  globalThis.clearTimeout = () => {}
  try {
    await withFakeWindow(async ({ window, parent }) => {
      const handle = makeNav().open('detail')
      const push = parent.messages.at(-1).data
      timers.find((timer) => timer.ms === 5000).cb()
      assert.deepEqual(await handle.outcome, { status: 'timeout' })
      assert.equal(await handle.ready, false)
      window.emit({ type: 'moebius:nav-push-ack', requestId: push.requestId })
      assert.equal(parent.messages.at(-1).data.type, 'moebius:nav-pop')
      assert.equal(parent.messages.filter((msg) => msg.data.type === 'moebius:nav-pop').length, 1)
      assert.deepEqual(await handle.outcome, { status: 'timeout' })
    })
  } finally {
    globalThis.setTimeout = previousSetTimeout
    globalThis.clearTimeout = previousClearTimeout
  }
})

test('nav helper reports a postMessage error without rejecting either promise', async () => {
  await withFakeWindow(async ({ parent }) => {
    parent.postMessage = () => { throw new Error('frame detached') }
    const handle = makeNav().open('detail')
    assert.deepEqual(await handle.outcome, { status: 'error' })
    assert.equal(await handle.ready, false)
  })
})

test('nav helper sends shell back only to the most recent owned entry', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const backed = []
    const nav = makeNav()
    const first = nav.open('first', () => backed.push('first'))
    const firstPush = parent.messages.at(-1).data
    window.emit({ type: 'moebius:nav-push-ack', requestId: firstPush.requestId })
    await first.outcome

    const second = nav.open('second', () => backed.push('second'))
    const secondPush = parent.messages.at(-1).data
    window.emit({ type: 'moebius:nav-push-ack', requestId: secondPush.requestId })
    await second.outcome

    window.emit({ type: 'moebius:nav-back' })
    assert.deepEqual(backed, ['second'])
    window.emit({ type: 'moebius:nav-back' })
    assert.deepEqual(backed, ['second', 'first'])
  })
})

test('signal helper queues bounded structured events instead of overwriting a file', async () => {
  const previousWindow = globalThis.window
  const previousDocument = globalThis.document
  const previousSetTimeout = globalThis.setTimeout
  const previousClearTimeout = globalThis.clearTimeout
  let flush
  const batches = []
  globalThis.window = { addEventListener() {} }
  globalThis.document = { visibilityState: 'visible', addEventListener() {} }
  globalThis.setTimeout = (cb) => { flush = cb; return 1 }
  globalThis.clearTimeout = () => {}
  try {
    const signal = makeSignal('7', {
      async _queueSignals(batch) { batches.push(batch) },
    })
    signal(' item_created ', {
      type: 'note',
      count: 2,
      nested: { ignored: true },
      infinite: Infinity,
    })
    flush()
    await new Promise((resolve) => setImmediate(resolve))

    assert.equal(batches.length, 1)
    assert.equal(batches[0].length, 1)
    assert.equal(batches[0][0].name, 'item_created')
    assert.match(batches[0][0].id, /\S+/)
    assert.match(batches[0][0].occurred_at, /^\d{4}-\d{2}-\d{2}T/)
    assert.deepEqual(batches[0][0].payload, { type: 'note', count: 2 })
  } finally {
    globalThis.window = previousWindow
    globalThis.document = previousDocument
    globalThis.setTimeout = previousSetTimeout
    globalThis.clearTimeout = previousClearTimeout
  }
})

test('signal helper never queues an event above the server ASCII byte budget', async () => {
  const previousWindow = globalThis.window
  const previousDocument = globalThis.document
  const previousSetTimeout = globalThis.setTimeout
  const previousClearTimeout = globalThis.clearTimeout
  let flush
  const batches = []
  globalThis.window = { addEventListener() {} }
  globalThis.document = { visibilityState: 'visible', addEventListener() {} }
  globalThis.setTimeout = (cb) => { flush = cb; return 1 }
  globalThis.clearTimeout = () => {}
  try {
    const signal = makeSignal('7', {
      async _queueSignals(batch) { batches.push(batch) },
    })
    const payload = Object.fromEntries(Array.from(
      { length: 20 },
      (_, index) => [
        `field-${index}`,
        index < 12 ? 1e-7 : '😀'.repeat(500),
      ],
    ))
    signal('large_unicode_event', payload)
    flush()
    await new Promise((resolve) => setImmediate(resolve))

    const serialized = JSON.stringify(batches[0][0]).replace(
      /[^\x00-\x7f]/g,
      (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`,
    )
    assert.ok(serialized.length <= 4000)
  } finally {
    globalThis.window = previousWindow
    globalThis.document = previousDocument
    globalThis.setTimeout = previousSetTimeout
    globalThis.clearTimeout = previousClearTimeout
  }
})

// ── makeChat embed bootstrap watchdog ────────────────────────────────────────
//
// A nested embed frame whose document never executes (an ancestor frame
// policy blocking it, a failed asset) posts NOTHING: no BOOTSTRAP_READY, no
// EMBED_ERROR — so without a watchdog the mount looks alive while the panel
// stays blank forever. These tests pin the bounded-silence contract: silence
// past the deadline emits the sticky 'error'; any authentic embed message
// stands the watchdog down.

function fakeEmbedDocument() {
  const frames = []
  const doc = {
    createElement(tag) {
      const el = {
        tag,
        style: {},
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value },
        addEventListener() {},
        removeEventListener() {},
        contentWindow: {
          messages: [],
          postMessage(data) { this.messages.push(data) },
        },
        parentNode: null,
      }
      if (tag === 'iframe') frames.push(el)
      return el
    },
  }
  const mount = {
    children: [],
    appendChild(el) {
      this.children.push(el)
      el.parentNode = this
    },
    removeChild(el) {
      this.children = this.children.filter((child) => child !== el)
      el.parentNode = null
    },
  }
  return { doc, mount, frames }
}

test('embedded chat mount reports a frame that never boots', async () => {
  await withFakeWindow(async () => {
    const { doc, mount } = fakeEmbedDocument()
    const previousDocument = globalThis.document
    globalThis.document = doc
    try {
      const chat = makeChat({
        appId: 81,
        getToken: async () => 'app-token',
        storage: null,
      })
      const handle = await chat({
        mount,
        chatId: 'chat-silent',
        bootstrapTimeoutMs: 20,
      })
      await new Promise((resolve) => setTimeout(resolve, 80))
      // Attach AFTER the deadline: the sticky emitter must replay the error
      // to a late listener, exactly like the mount-time READY contract.
      const seen = []
      handle.on('error', (detail) => seen.push(detail))
      assert.equal(seen.length, 1)
      assert.equal(seen[0].chatId, 'chat-silent')
      assert.match(seen[0].error, /did not start/)
      handle.destroy()
    } finally {
      globalThis.document = previousDocument
    }
  })
})

test('embedded chat watchdog stands down once the frame posts', async () => {
  await withFakeWindow(async ({ window: fakeWindow }) => {
    const { doc, mount, frames } = fakeEmbedDocument()
    const previousDocument = globalThis.document
    globalThis.document = doc
    try {
      const chat = makeChat({
        appId: 82,
        getToken: async () => 'app-token',
        storage: null,
      })
      const handle = await chat({
        mount,
        chatId: 'chat-booted',
        bootstrapTimeoutMs: 30,
      })
      const seen = []
      handle.on('error', (detail) => seen.push(detail))
      // The child's first authentic message (bootstrap-ready from the exact
      // mounted frame) proves the document booted.
      fakeWindow.emit(
        { type: 'moebius:chat-embed:bootstrap-ready' },
        { origin: 'null', source: frames[0].contentWindow },
      )
      await new Promise((resolve) => setTimeout(resolve, 90))
      assert.equal(
        seen.filter((detail) => /did not start/.test(detail.error)).length,
        0,
      )
      handle.destroy()
    } finally {
      globalThis.document = previousDocument
    }
  })
})
