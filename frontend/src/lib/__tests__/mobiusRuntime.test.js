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
import { overlayPending } from '../../../public/mobius-runtime.js'

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
