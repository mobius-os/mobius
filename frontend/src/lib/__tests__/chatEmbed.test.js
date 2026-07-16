/**
 * Unit tests for the agent-chat embed protocol (capability A, design §1).
 *
 * Run with the lib loader (src/lib is in the test:lib glob):
 *   cd frontend && npm run test:lib
 * or directly:
 *   node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/lib/__tests__/chatEmbed.test.js
 *
 * The §1.4 hardening — validate BOTH origin AND e.source, plus the
 * instanceId correlation — is the security-load-bearing logic here:
 * three same-origin frames mean origin alone can't tell our embed from
 * a sibling frame. These tests pin that guard so a future edit that
 * loosens it (e.g. drops the source check) fails loudly. The harness
 * has no DOM, so we exercise the pure functions with plain event-shaped
 * objects rather than real MessageEvents.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  NS, INIT, READY, MESSAGE_SENT, TURN_DONE, ERROR, HEIGHT,
  isEmbedMessage, embedUrl, makeEmitter,
} from '../chatEmbed.js'

const ORIGIN = 'https://mobius.example'
const SRC = { name: 'embed-frame' } // stand-in for a contentWindow / window

function evt({ origin = ORIGIN, source = SRC, type, instanceId, chatId } = {}) {
  return { origin, source, data: { type, instanceId, chatId } }
}

test('all message types share the moebius:chat-embed: namespace', () => {
  for (const t of [INIT, READY, MESSAGE_SENT, TURN_DONE, ERROR, HEIGHT]) {
    assert.ok(t.startsWith(NS), `${t} must start with ${NS}`)
  }
  // Distinct from the app-frame protocol so a stray frame message can
  // never be mistaken for an embed message on the shared origin.
  assert.ok(!NS.startsWith('moebius:frame'))
  assert.ok(!NS.startsWith('moebius:nav'))
})

test('isEmbedMessage accepts a matching same-origin, same-source, correlated message', () => {
  const e = evt({ type: TURN_DONE, instanceId: 'app-1:1:99' })
  assert.equal(
    isEmbedMessage(e, { origin: ORIGIN, expectedSource: SRC, instanceId: 'app-1:1:99' }),
    true,
  )
})

test('isEmbedMessage rejects a cross-origin message even with the right source', () => {
  const e = evt({ origin: 'https://evil.example', type: READY, instanceId: 'i' })
  assert.equal(
    isEmbedMessage(e, { origin: ORIGIN, expectedSource: SRC, instanceId: 'i' }),
    false,
  )
})

test('isEmbedMessage accepts an opaque parent only when explicitly allowed and source matches', () => {
  const opaque = evt({ origin: 'null', type: INIT, instanceId: 'i' })
  assert.equal(
    isEmbedMessage(opaque, {
      origin: ORIGIN,
      expectedSource: SRC,
      allowOpaqueOrigin: true,
    }),
    true,
  )
  assert.equal(
    isEmbedMessage(opaque, { origin: ORIGIN, expectedSource: SRC }),
    false,
  )
  assert.equal(
    isEmbedMessage(opaque, {
      origin: ORIGIN,
      expectedSource: { name: 'sibling' },
      allowOpaqueOrigin: true,
    }),
    false,
  )
})

test('isEmbedMessage rejects a sibling same-origin frame (wrong source)', () => {
  // The whole point of §1.4: another frame shares the origin. A message
  // from a DIFFERENT window must be ignored even though origin matches.
  const sibling = { name: 'other-frame' }
  const e = evt({ source: sibling, type: MESSAGE_SENT, instanceId: 'i' })
  assert.equal(
    isEmbedMessage(e, { origin: ORIGIN, expectedSource: SRC, instanceId: 'i' }),
    false,
  )
})

test('isEmbedMessage rejects a message for a different embed instance', () => {
  const e = evt({ type: READY, instanceId: 'app-1:2:other' })
  assert.equal(
    isEmbedMessage(e, { origin: ORIGIN, expectedSource: SRC, instanceId: 'app-1:1:mine' }),
    false,
  )
})

test('isEmbedMessage rejects a foreign namespace and non-object data', () => {
  const foreign = { origin: ORIGIN, source: SRC, data: { type: 'moebius:frame-init' } }
  assert.equal(isEmbedMessage(foreign, { origin: ORIGIN, expectedSource: SRC }), false)
  const notObject = { origin: ORIGIN, source: SRC, data: 'moebius:chat-embed:ready' }
  assert.equal(isEmbedMessage(notObject, { origin: ORIGIN, expectedSource: SRC }), false)
  const noData = { origin: ORIGIN, source: SRC, data: null }
  assert.equal(isEmbedMessage(noData, { origin: ORIGIN, expectedSource: SRC }), false)
})

test('isEmbedMessage skips the source check when expectedSource is absent', () => {
  // The child can only learn its instanceId from INIT, so the first
  // inbound message is validated by source+origin with the instanceId
  // check skipped (instanceId omitted by the caller). Source still
  // matters; origin still matters.
  const e = evt({ type: INIT, instanceId: 'app-1:1:99' })
  assert.equal(
    isEmbedMessage(e, { origin: ORIGIN, expectedSource: SRC /* no instanceId */ }),
    true,
  )
})

test('embedUrl builds the route with and without a chatId, honoring base', () => {
  assert.equal(embedUrl(), '/shell/embed/chat')
  assert.equal(embedUrl({ chatId: 'abc' }), '/shell/embed/chat?chatId=abc')
  // A chatId with URL-significant characters is encoded.
  assert.equal(embedUrl({ chatId: 'a b/c' }), '/shell/embed/chat?chatId=a%20b%2Fc')
  // Deploy prefix (e.g. /proxy/8001) is prepended.
  assert.equal(embedUrl({ base: '/proxy/8001', chatId: 'x' }), '/proxy/8001/shell/embed/chat?chatId=x')
})

// makeEmitter is the sticky-emit core that mobius-runtime.js's makeChat
// uses (and mirrors). makeChat depends on a DOM (iframe, postMessage,
// fetch) the lib harness has no jsdom for, so we unit-test the pure core
// directly — the same emit/on the handle delegates to.

test('makeEmitter delivers an event to a listener registered before it fires', () => {
  const { emit, on } = makeEmitter()
  const seen = []
  on('turn-done', (d) => seen.push(d))
  emit('turn-done', { chatId: 'c1' })
  assert.deepEqual(seen, [{ chatId: 'c1' }])
})

test("makeEmitter replays a 'ready' that already fired to a late handler (the early-ready drop fix)", () => {
  // The child posts its mount-time READY before the app — which only gets
  // the handle AFTER `await chat(...)` — can attach a listener. Without
  // sticky replay, a handler attached right after the await misses it.
  const { emit, on } = makeEmitter()
  emit('ready', { chatId: 'c1' })
  const seen = []
  on('ready', (d) => seen.push(d)) // attached AFTER ready already emitted
  assert.deepEqual(seen, [{ chatId: 'c1' }], 'late ready handler must still observe the ready')
})

test("makeEmitter replays the LATEST sticky detail, and 'error' is sticky too", () => {
  const { emit, on } = makeEmitter()
  emit('ready', { chatId: 'first' })
  emit('ready', { chatId: 'second' })
  emit('error', { chatId: 'c1', error: 'boom' })
  const readySeen = []
  const errSeen = []
  on('ready', (d) => readySeen.push(d))
  on('error', (d) => errSeen.push(d))
  assert.deepEqual(readySeen, [{ chatId: 'second' }], 'replays the most recent ready')
  assert.deepEqual(errSeen, [{ chatId: 'c1', error: 'boom' }], 'error is sticky and replays')
})

test("makeEmitter does NOT replay a repeatable 'turn-done' to a late handler", () => {
  // turn-done / message-sent fire once per turn — replaying a past one to a
  // newly-attached handler would double-fire. They are deliberately not sticky.
  const { emit, on } = makeEmitter()
  emit('turn-done', { chatId: 'c1' })
  const seen = []
  on('turn-done', (d) => seen.push(d)) // attached AFTER a turn-done fired
  assert.deepEqual(seen, [], 'a past turn-done must not replay to a late listener')
})

test('makeEmitter still fires sticky events live to handlers attached before they fire (no double-fire)', () => {
  // A handler present at emit time gets exactly one call — the replay path
  // must not pile a second delivery onto an already-notified early listener.
  const { emit, on } = makeEmitter()
  const seen = []
  on('ready', (d) => seen.push(d))
  emit('ready', { chatId: 'c1' })
  assert.deepEqual(seen, [{ chatId: 'c1' }], 'exactly one delivery for an early ready listener')
})
