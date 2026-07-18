import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const frame = readFileSync(
  new URL('../../../public/app-frame.html', import.meta.url),
  'utf8',
)
const canvas = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url),
  'utf8',
)
const bridgeSource = frame.match(
  /<script data-mobius-storage-rpc>([\s\S]*?)<\/script>/,
)?.[1]

function harness({ throwOnPost = null } = {}) {
  const listeners = new Map()
  const timers = new Map()
  const posts = []
  let nextTimer = 1
  const parent = {
    postMessage(message, origin) {
      if (throwOnPost) throw throwOnPost
      posts.push({ message, origin })
    },
  }
  const window = {
    parent,
    location: { origin: 'https://mobius.test' },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(listener)
    },
  }
  runInNewContext(bridgeSource, {
    window,
    Map,
    Promise,
    Error,
    Date,
    Array,
    Object,
    setTimeout(callback) {
      const id = nextTimer++
      timers.set(id, callback)
      return id
    },
    clearTimeout(id) { timers.delete(id) },
  })
  return {
    window,
    parent,
    posts,
    timerCount: () => timers.size,
    message(source, data, origin = 'https://mobius.test') {
      for (const listener of listeners.get('message') || []) {
        listener({ source, origin, data })
      }
    },
  }
}

test('frame bridge removes its pending request when postMessage throws', async () => {
  assert.ok(bridgeSource, 'storage RPC script is present in app-frame.html')
  const h = harness({ throwOnPost: new Error('frame detached') })
  await assert.rejects(
    h.window.__mobiusStorageBridgeCall('get', ['note.json']),
    /frame detached/,
  )
  assert.equal(h.timerCount(), 0)
})

test('AppCanvas routes storage only after exact frame-source attribution', () => {
  const sourceGate = canvas.indexOf('if (srcVersion == null) return')
  const storageRpc = canvas.indexOf("if (msg.type === 'moebius:storage-rpc')")
  assert.ok(sourceGate >= 0 && storageRpc > sourceGate)
  assert.match(canvas, /createAppStorageHost\(/)
  assert.match(canvas, /storageHostRef\.current\.detachSource/)
  const appFrame = canvas.slice(canvas.indexOf('data-frame-version={v}'))
  const sandbox = appFrame.match(/sandbox="([^"]+)"/)?.[1] || ''
  assert.match(sandbox, /\ballow-scripts\b/)
  assert.doesNotMatch(sandbox, /\ballow-same-origin\b/)
})

test('frame bridge accepts results and changes only from its exact parent', async () => {
  const h = harness()
  const result = h.window.__mobiusStorageBridgeCall('get', ['note.json'])
  const requestId = h.posts.at(-1).message.requestId
  h.message({}, { type: 'moebius:storage-rpc-result', requestId, ok: true, result: 'wrong' })
  h.message(h.parent, { type: 'moebius:storage-rpc-result', requestId, ok: true, result: 'saved' })
  assert.equal(await result, 'saved')
  assert.equal(h.timerCount(), 0)

  const values = []
  const unsubscribe = h.window.__mobiusStorageBridgeSubscribe(
    'text', 'note.txt', (value) => values.push(value),
  )
  const subscribePost = h.posts.at(-1).message
  const subscriptionId = subscribePost.args[0]
  h.message({}, { type: 'moebius:storage-change', subscriptionId, value: 'wrong' })
  h.message(h.parent, { type: 'moebius:storage-change', subscriptionId, value: 'current' })
  assert.deepEqual(values, ['current'])

  unsubscribe()
  h.message(h.parent, { type: 'moebius:storage-change', subscriptionId, value: 'late' })
  assert.deepEqual(values, ['current'])
  assert.equal(h.posts.at(-1).message.method, 'unsubscribe')
})
