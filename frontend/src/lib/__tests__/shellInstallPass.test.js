import test from 'node:test'
import assert from 'node:assert/strict'

import { createShellInstallPassPreparer } from '../shellInstallPassPreparer.js'

function preparer(overrides = {}) {
  let calls = 0
  const prepare = createShellInstallPassPreparer({
    request: async () => { calls += 1; return { ok: true } },
    isIos: () => true,
    isStandalone: () => false,
    hasToken: () => true,
    ...overrides,
  })
  return { ...prepare, calls: () => calls }
}

test('prepares once and refreshes only when installation intent is explicit', async () => {
  const h = preparer()
  assert.equal(await h.prepare(), true)
  assert.equal(await h.prepare(), true)
  assert.equal(h.calls(), 1)
  assert.equal(await h.prepare({ force: true }), true)
  assert.equal(h.calls(), 2)
})

test('shares concurrent preparation and leaves failure retryable', async () => {
  let release
  let calls = 0
  const first = new Promise(resolve => { release = resolve })
  const h = preparer({
    request: async () => {
      calls += 1
      if (calls === 1) return first
      return { ok: true }
    },
  })
  const one = h.prepare()
  const two = h.prepare({ force: true })
  release({ ok: false })
  assert.deepEqual(await Promise.all([one, two]), [false, false])
  assert.equal(await h.prepare(), true)
  assert.equal(calls, 2)
})

test('never creates a handoff outside an authenticated iOS browser tab', async () => {
  for (const overrides of [
    { isIos: () => false },
    { isStandalone: () => true },
    { hasToken: () => false },
  ]) {
    const h = preparer(overrides)
    assert.equal(await h.prepare(), false)
    assert.equal(h.calls(), 0)
  }
})

test('lifecycle stop settles a transport that would otherwise never finish', async () => {
  let calls = 0
  const h = preparer({
    request: ({ signal }) => {
      calls += 1
      return new Promise((_resolve, reject) => {
        if (signal.aborted) {
          reject(signal.reason)
          return
        }
        signal.addEventListener('abort', () => reject(signal.reason), { once: true })
      })
    },
  })
  const pending = h.prepare()
  await h.stop()

  assert.equal(await pending, false)
  assert.equal(calls, 1)
  assert.equal(await h.prepare({ force: true }), false)
  assert.equal(calls, 1)
})

test('caller cancellation settles a transport that would otherwise never finish', async () => {
  const h = preparer({
    request: ({ signal }) => new Promise((_resolve, reject) => {
      if (signal.aborted) {
        reject(signal.reason)
        return
      }
      signal.addEventListener('abort', () => reject(signal.reason), { once: true })
    }),
  })
  const controller = new AbortController()
  const pending = h.prepare({ signal: controller.signal })
  controller.abort()

  assert.equal(await pending, false)
})
