import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createAppStorageHost } from '../appStorageHost.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function identity(token) {
  const [appId, nonce] = String(token || '').split(':')
  return appId ? { appId, appInstanceId: nonce || null } : null
}

function fakeStorage(name) {
  const subscriptions = new Map()
  const storage = {
    name,
    destroyed: false,
    getCalls: 0,
    async get(path) { storage.getCalls += 1; return `${name}:${path}` },
    async durableWrite(path, value) {
      storage.emit(path, value)
      return { durability: 'synced', path, writeId: 'test-write' }
    },
    async _drain() { return name },
    onDeadLetter() { return () => {} },
    subscribe(path, cb) {
      if (!subscriptions.has(path)) subscriptions.set(path, new Set())
      subscriptions.get(path).add(cb)
      return () => subscriptions.get(path)?.delete(cb)
    },
    subscribeText(path, cb) { return storage.subscribe(path, cb) },
    subscribeBlob(path, cb) { return storage.subscribe(path, cb) },
    emit(path, value) {
      for (const cb of subscriptions.get(path) || []) cb(value)
    },
    subscriberCount(path) { return subscriptions.get(path)?.size || 0 },
    _destroy() { storage.destroyed = true; subscriptions.clear() },
  }
  return storage
}

function harness({ createStorage } = {}) {
  let token = '7:install-a'
  const creations = []
  const sent = []
  const host = createAppStorageHost({
    appId: '7',
    getCurrentToken: () => token,
    getToken: async () => token,
    tokenIdentity: identity,
    createStorage: createStorage || (async (options) => {
      const storage = fakeStorage(options.appInstanceId)
      creations.push({ options, storage })
      return storage
    }),
    send(source, message) { sent.push({ source, message }); return true },
  })
  return {
    host,
    creations,
    sent,
    setToken(next) { token = next },
  }
}

test('one app installation reuses one host storage runtime', async () => {
  const h = harness()
  const source = {}
  assert.equal(await h.host.handleRpc(source, 'get', ['one.json']), 'install-a:one.json')
  assert.equal(await h.host.handleRpc(source, 'get', ['two.json']), 'install-a:two.json')
  assert.equal(h.creations.length, 1)
})

test('an in-flight old identity is discarded and cannot borrow the rotated token', async () => {
  const oldCreation = deferred()
  const oldStorage = fakeStorage('install-a')
  const newStorage = fakeStorage('install-b')
  const brokers = []
  const h = harness({
    async createStorage(options) {
      brokers.push(options.getToken)
      if (options.appInstanceId === 'install-a') return oldCreation.promise
      return newStorage
    },
  })
  const source = {}
  const first = h.host.handleRpc(source, 'get', ['first.json'])
  await Promise.resolve()
  h.setToken('7:install-b')
  const second = h.host.handleRpc(source, 'get', ['second.json'])

  assert.equal(await brokers[0](), null, 'old runtime rejects the new installation token')
  oldCreation.resolve(oldStorage)
  assert.equal(await first, 'install-b:first.json')
  assert.equal(await second, 'install-b:second.json')
  assert.equal(oldStorage.destroyed, true)
  assert.equal(newStorage.destroyed, false)
})

test('subscriptions reattach on identity rotation and old callbacks go silent', async () => {
  const h = harness()
  const source = {}
  await h.host.handleRpc(source, 'subscribe', ['sub-1', 'note.json', 'json'])
  const oldStorage = h.creations[0].storage
  oldStorage.emit('note.json', { version: 'old' })
  assert.deepEqual(h.sent.at(-1).message.value, { version: 'old' })

  h.setToken('7:install-b')
  const deliveredBeforeReconcile = h.sent.length
  oldStorage.emit('note.json', { stale: 'identity-transition' })
  assert.equal(h.sent.length, deliveredBeforeReconcile)
  await h.host.reconcile()
  const newStorage = h.creations[1].storage
  assert.equal(oldStorage.destroyed, true)
  assert.equal(newStorage.subscriberCount('note.json'), 1)

  const delivered = h.sent.length
  oldStorage.emit('note.json', { stale: true })
  assert.equal(h.sent.length, delivered)
  newStorage.emit('note.json', { version: 'new' })
  assert.deepEqual(h.sent.at(-1).message.value, { version: 'new' })
})

test('unsubscribe during creation cannot attach a late subscription', async () => {
  const creation = deferred()
  const storage = fakeStorage('install-a')
  const h = harness({ createStorage: async () => creation.promise })
  const source = {}
  const pending = h.host.handleRpc(source, 'subscribe', ['sub-1', 'note.json', 'json'])
  await Promise.resolve()
  assert.equal(await h.host.handleRpc(source, 'unsubscribe', ['sub-1']), true)
  creation.resolve(storage)
  assert.equal(await pending, true)
  assert.equal(storage.subscriberCount('note.json'), 0)
})

test('detaching an exact frame source removes only its subscriptions', async () => {
  const h = harness()
  const sourceA = {}
  const sourceB = {}
  await h.host.handleRpc(sourceA, 'subscribe', ['a', 'note.json', 'text'])
  await h.host.handleRpc(sourceB, 'subscribe', ['b', 'note.json', 'text'])
  const storage = h.creations[0].storage
  assert.equal(storage.subscriberCount('note.json'), 2)

  h.host.detachSource(sourceA)
  assert.equal(storage.subscriberCount('note.json'), 1)
  storage.emit('note.json', 'kept')
  assert.equal(h.sent.some(({ source }) => source === sourceA), false)
  assert.equal(h.sent.at(-1).source, sourceB)
})

test('a write from one buffered frame updates every subscribed sibling frame', async () => {
  const h = harness()
  const sourceA = {}
  const sourceB = {}
  await h.host.handleRpc(sourceA, 'subscribe', ['a', 'note.json', 'json'])
  await h.host.handleRpc(sourceB, 'subscribe', ['b', 'note.json', 'json'])

  await h.host.handleRpc(sourceA, 'durableWrite', ['note.json', { body: 'shared' }])

  const changes = h.sent.filter(({ message }) => message.type === 'moebius:storage-change')
  assert.equal(changes.length, 2)
  assert.deepEqual(new Set(changes.map(({ source }) => source)), new Set([sourceA, sourceB]))
  assert.equal(changes.every(({ message }) => message.value.body === 'shared'), true)
})

test('an unavailable or cross-app token fails closed', async () => {
  const h = harness()
  await assert.rejects(
    h.host.handleRpc({}, 'set', ['private.json', { denied: true }]),
    (error) => error.code === 'storage_method_denied',
  )
  h.setToken('8:other-app')
  await assert.rejects(
    h.host.handleRpc({}, 'get', ['private.json']),
    (error) => error.code === 'storage_identity_unavailable',
  )
  assert.equal(h.creations.length, 0)
})

test('destroying during creation retires the late runtime and rejects the caller', async () => {
  const creation = deferred()
  const storage = fakeStorage('install-a')
  const h = harness({ createStorage: async () => creation.promise })
  const pending = h.host.handleRpc({}, 'get', ['note.json'])
  await Promise.resolve()

  h.host.destroy()
  creation.resolve(storage)

  await assert.rejects(pending, /shell host is detached/)
  assert.equal(storage.destroyed, true)
})

test('a failed creation does not wedge a later retry', async () => {
  let attempts = 0
  const h = harness({
    async createStorage(options) {
      attempts += 1
      if (attempts === 1) throw new Error('module load failed')
      return fakeStorage(options.appInstanceId)
    },
  })

  await assert.rejects(h.host.handleRpc({}, 'get', ['first.json']), /module load failed/)
  assert.equal(await h.host.handleRpc({}, 'get', ['second.json']), 'install-a:second.json')
  assert.equal(attempts, 2)
})
