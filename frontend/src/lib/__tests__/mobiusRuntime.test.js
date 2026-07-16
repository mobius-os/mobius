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
  makeMicrophone,
  makeNav,
  makeSignal,
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
    assert.deepEqual(await handle.outcome, { status: 'owned' })
    assert.equal(await handle.ready, true)

    window.emit({ type: 'moebius:nav-back' })
    assert.equal(backed, true)
    assert.equal(parent.messages.some((msg) => msg.data.type === 'moebius:nav-pop'), false)
  })
})

test('microphone bridge correlates shell capture and exposes PCM to the app', async () => {
  await withFakeWindow(async ({ window, parent }) => {
    const levels = []
    const session = makeMicrophone().start({ maxSeconds: 8, onLevel: (v) => levels.push(v) })
    const start = parent.messages.at(-1).data
    assert.equal(start.type, 'moebius:microphone-start')
    assert.equal(start.maxSeconds, 8)

    window.emit({
      type: 'moebius:microphone-started', requestId: start.requestId, sampleRate: 48000,
    })
    assert.deepEqual(await session.started, { sampleRate: 48000 })
    window.emit({
      type: 'moebius:microphone-level', requestId: start.requestId, level: 0.6,
    })
    session.stop()
    assert.equal(parent.messages.at(-1).data.type, 'moebius:microphone-stop')

    const samples = new Float32Array([0.1, -0.2, 0.3])
    window.emit({
      type: 'moebius:microphone-result', requestId: start.requestId,
      sampleRate: 48000, samples,
    })
    const result = await session.done
    assert.equal(result.sampleRate, 48000)
    assert.deepEqual([...result.samples], [...samples])
    assert.deepEqual(levels, [0.6])
  })
})

test('microphone bridge can cancel while permission is still pending', async () => {
  await withFakeWindow(async ({ parent }) => {
    const session = makeMicrophone().start({ maxSeconds: 4 })
    session.cancel()
    assert.equal(parent.messages.at(-1).data.type, 'moebius:microphone-cancel')
    await assert.rejects(session.started, { name: 'AbortError' })
    await assert.rejects(session.done, { name: 'AbortError' })
  })
})

test('standalone microphone can stop while permission is still pending', async () => {
  const previousWindow = globalThis.window
  const previousNavigator = globalThis.navigator
  let grant
  let trackStopped = false
  const node = () => ({ connect() {}, disconnect() {} })
  class FakeAudioContext {
    constructor() { this.sampleRate = 8000; this.state = 'running'; this.destination = node() }
    createMediaStreamSource() { return node() }
    createScriptProcessor() { return { ...node(), onaudioprocess: null } }
    createGain() { return { ...node(), gain: { value: 1 } } }
    close() {}
  }
  const standalone = {
    location: { origin: 'https://mobius.test' },
    AudioContext: FakeAudioContext,
    addEventListener() {},
    removeEventListener() {},
  }
  standalone.parent = standalone
  globalThis.window = standalone
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      mediaDevices: {
        getUserMedia: () => new Promise((resolve) => { grant = resolve }),
      },
    },
  })
  try {
    const session = makeMicrophone().start({ maxSeconds: 2 })
    session.stop()
    grant({ getTracks: () => [{ stop: () => { trackStopped = true } }] })
    assert.deepEqual(await session.started, { sampleRate: 8000 })
    const result = await session.done
    assert.equal(result.sampleRate, 8000)
    assert.equal(result.samples.length, 0)
    assert.equal(trackStopped, true)
  } finally {
    globalThis.window = previousWindow
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: previousNavigator,
    })
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

test('nav helper distinguishes standalone fallback from shell failure', async () => {
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
    assert.deepEqual(await handle.outcome, { status: 'standalone' })
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
