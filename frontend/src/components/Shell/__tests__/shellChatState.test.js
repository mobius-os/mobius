import test from 'node:test'
import assert from 'node:assert/strict'
import { watchChatStateOnResume } from '../shellChatState.js'

function target(extra = {}) {
  const listeners = new Map()
  return {
    ...extra,
    addEventListener(type, fn) { listeners.set(type, fn) },
    removeEventListener(type, fn) {
      if (listeners.get(type) === fn) listeners.delete(type)
    },
    fire(type) { return listeners.get(type)?.() },
    has(type) { return listeners.has(type) },
  }
}

test('resume reconciles once across overlapping browser signals', async () => {
  const doc = target({ visibilityState: 'visible' })
  const win = target()
  let release
  let calls = 0
  const pending = new Promise(resolve => { release = resolve })
  const dispose = watchChatStateOnResume({
    doc, win, reconcile: () => { calls += 1; return pending },
  })
  const first = win.fire('focus')
  const second = doc.fire('visibilitychange')
  await Promise.resolve()
  assert.equal(calls, 1)
  assert.equal(first, second)
  release()
  await first
  await win.fire('pageshow')
  assert.equal(calls, 2)
  dispose()
  assert.equal(doc.has('visibilitychange'), false)
  assert.equal(win.has('focus'), false)
})

test('hidden and disposed pages do not reconcile', async () => {
  const doc = target({ visibilityState: 'hidden' })
  const win = target()
  let calls = 0
  const dispose = watchChatStateOnResume({
    doc, win, reconcile: () => { calls += 1 },
  })
  await win.fire('focus')
  assert.equal(calls, 0)
  doc.visibilityState = 'visible'
  dispose()
  await win.fire('pageshow')
  assert.equal(calls, 0)
})
