import { createHash, webcrypto } from 'node:crypto'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createDeviceAssetCacheProvider,
  normalizeDeviceAssetPackage,
  purgeDeviceAssetCache,
} from '../deviceAssetCache.js'

const DECLARATION = {
  limits: {
    max_bytes: 1024,
    max_asset_bytes: 1024,
    max_chunk_bytes: 512,
  },
}

function digest(bytes) {
  return createHash('sha256').update(bytes).digest('hex')
}

function packageInput(chunks = [Buffer.from('hello'), Buffer.from(' world')]) {
  return {
    operation: 'install',
    package: {
      key: 'voice-v1',
      assets: [{
        id: 'model',
        url: 'https://assets.example/model.bin',
        bytes: chunks.reduce((total, chunk) => total + chunk.byteLength, 0),
        chunks: chunks.map((chunk) => ({ bytes: chunk.byteLength, sha256: digest(chunk) })),
      }],
    },
  }
}

class MemoryCache {
  constructor() { this.values = new Map() }
  async match(key) {
    const response = this.values.get(typeof key === 'string' ? key : key.url)
    return response?.clone()
  }
  async put(key, response) {
    this.values.set(typeof key === 'string' ? key : key.url, response.clone())
  }
  async delete(key) {
    return this.values.delete(typeof key === 'string' ? key : key.url)
  }
  async keys() {
    return [...this.values.keys()].map((url) => new Request(url))
  }
}

class MemoryCacheStorage {
  constructor() { this.values = new Map() }
  async open(name) {
    if (!this.values.has(name)) this.values.set(name, new MemoryCache())
    return this.values.get(name)
  }
  async keys() { return [...this.values.keys()] }
  async delete(name) { return this.values.delete(name) }
}

function runProvider(provider, input, { events = [], ready = [] } = {}) {
  return new Promise((resolve, reject) => {
    let control
    control = provider.open({
      input,
      declaration: DECLARATION,
      channel: {
        ready(value) { ready.push(value) },
        event(name, value, transfer) {
          events.push({ name, value, transfer })
          if (name === 'chunk') queueMicrotask(() => control.control('next'))
        },
        result: resolve,
        error: reject,
      },
    })
  })
}

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 1))
  }
  throw new Error('Timed out waiting for device asset test state.')
}

test('device package validation applies reviewed package and chunk limits', () => {
  const normalized = normalizeDeviceAssetPackage(packageInput(), DECLARATION)
  assert.equal(normalized.bytes, 11)
  assert.deepEqual(normalized.assets[0].chunks.map((chunk) => chunk.offset), [0, 5])

  const tooLarge = packageInput([Buffer.alloc(513)])
  assert.throws(
    () => normalizeDeviceAssetPackage(tooLarge, DECLARATION),
    (error) => error.code === 'invalid_request' && /chunk/.test(error.message),
  )
})

test('status does not create browser storage before an explicit install', async () => {
  const cacheStorage = new MemoryCacheStorage()
  const provider = createDeviceAssetCacheProvider({
    appId: '61',
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
  })
  const result = await runProvider(
    provider,
    { ...packageInput(), operation: 'status' },
  )

  assert.equal(result.state, 'missing')
  assert.equal(cacheStorage.values.size, 0)
})

test('install verifies and resumes chunks, then read transfers them in order', async () => {
  const cacheStorage = new MemoryCacheStorage()
  const chunks = [Buffer.from('hello'), Buffer.from(' world')]
  const calls = []
  const provider = createDeviceAssetCacheProvider({
    appId: 61,
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {
      async persisted() { return false },
      async persist() { return true },
      async estimate() { return { usage: 0, quota: 1_000_000 } },
    },
    async fetchRange({ offset }) {
      calls.push(offset)
      return new Response(chunks[offset === 0 ? 0 : 1])
    },
  })
  const input = packageInput(chunks)
  const events = []
  const installed = await runProvider(provider, input, { events })
  assert.equal(installed.state, 'ready')
  assert.equal(installed.persistence, 'persistent')
  assert.deepEqual(calls, [0, 5])
  assert.deepEqual(events.map(({ value }) => value.downloadedBytes), [5, 11])

  await runProvider(provider, input)
  assert.deepEqual(calls, [0, 5], 'verified chunks are reused on a repeated install')

  const readEvents = []
  const readResult = await runProvider(
    provider,
    { ...input, operation: 'read' },
    { events: readEvents },
  )
  assert.equal(readResult.state, 'ready')
  assert.deepEqual(
    readEvents.map(({ value }) => Buffer.from(value.bytes).toString()),
    ['hello', ' world'],
  )
  assert.equal(readEvents.every(({ value, transfer }) => transfer[0] === value.bytes), true)
})

test('read waits for each consumer acknowledgement before transferring the next chunk', async () => {
  const cacheStorage = new MemoryCacheStorage()
  const chunks = [Buffer.from('first'), Buffer.from('second')]
  const provider = createDeviceAssetCacheProvider({
    appId: 61,
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
    async fetchRange({ offset }) {
      return new Response(chunks[offset === 0 ? 0 : 1])
    },
  })
  const input = packageInput(chunks)
  await runProvider(provider, input)

  const events = []
  let control
  const result = new Promise((resolve, reject) => {
    control = provider.open({
      input: { ...input, operation: 'read' },
      declaration: DECLARATION,
      channel: {
        ready() {},
        event(name, value) { if (name === 'chunk') events.push(value) },
        result: resolve,
        error: reject,
      },
    })
  })
  await waitFor(() => events.length === 1)
  assert.equal(events.length, 1)

  control.control('next')
  await waitFor(() => events.length === 2)
  assert.equal(events.length, 2)
  control.control('next')
  await result
})

test('an invalid download is rejected without replacing a complete package', async () => {
  const cacheStorage = new MemoryCacheStorage()
  const original = [Buffer.from('safe')]
  const provider = createDeviceAssetCacheProvider({
    appId: 61,
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
    async fetchRange() { return new Response(original[0]) },
  })
  const first = packageInput(original)
  await runProvider(provider, first)

  const replacement = packageInput([Buffer.from('new!')])
  replacement.package.assets[0].url = 'https://assets.example/model-v2.bin'
  const brokenProvider = createDeviceAssetCacheProvider({
    appId: 61,
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
    async fetchRange() { return new Response(Buffer.from('bad!')) },
  })
  await assert.rejects(
    runProvider(brokenProvider, replacement),
    (error) => error.code === 'integrity_failed',
  )

  const readEvents = []
  await runProvider(provider, { ...first, operation: 'read' }, { events: readEvents })
  assert.equal(Buffer.from(readEvents[0].value.bytes).toString(), 'safe')
})

test('install rejects an upstream asset whose total differs from the reviewed size', async () => {
  const chunks = [Buffer.from('hello'), Buffer.from(' world')]
  const provider = createDeviceAssetCacheProvider({
    appId: 61,
    cacheStorage: new MemoryCacheStorage(),
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
    async fetchRange({ offset }) {
      return new Response(chunks[offset === 0 ? 0 : 1], {
        headers: { 'X-Mobius-Asset-Total': '12' },
      })
    },
  })

  await assert.rejects(
    runProvider(provider, packageInput(chunks)),
    (error) => error.code === 'download_failed' && /reviewed asset size/.test(error.message),
  )
})

test('separate complete packages cannot exceed the reviewed app partition', async () => {
  const cacheStorage = new MemoryCacheStorage()
  const declaration = {
    limits: { max_bytes: 10, max_asset_bytes: 10, max_chunk_bytes: 10 },
  }
  let body = Buffer.from('123456')
  const provider = createDeviceAssetCacheProvider({
    appId: 61,
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
    async fetchRange() { return new Response(body) },
  })
  const first = packageInput([body])
  await new Promise((resolve, reject) => provider.open({
    input: first,
    declaration,
    channel: { ready() {}, event() {}, result: resolve, error: reject },
  }))

  body = Buffer.from('abcdef')
  const second = packageInput([body])
  second.package.key = 'another-package'
  await assert.rejects(
    new Promise((resolve, reject) => provider.open({
      input: second,
      declaration,
      channel: { ready() {}, event() {}, result: resolve, error: reject },
    })),
    (error) => error.code === 'quota_exceeded',
  )
})

test('explicit app-data purge removes only that app device partition', async () => {
  const cacheStorage = new MemoryCacheStorage()
  await cacheStorage.open('mobius-device-assets-v1-app-61')
  await cacheStorage.open('mobius-device-assets-v1-app-62')

  assert.equal(await purgeDeviceAssetCache(61, cacheStorage), true)
  assert.equal(cacheStorage.values.has('mobius-device-assets-v1-app-61'), false)
  assert.equal(cacheStorage.values.has('mobius-device-assets-v1-app-62'), true)
})

test('a reviewed shared partition can be read outside an app after its manager installs it', async () => {
  const cacheStorage = new MemoryCacheStorage()
  const chunks = [Buffer.from('shared voice')]
  const manager = createDeviceAssetCacheProvider({
    appId: 61,
    partitionId: 'speech-v1',
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
    async fetchRange() { return new Response(chunks[0]) },
  })
  const input = packageInput(chunks)
  await runProvider(manager, input)

  const shell = createDeviceAssetCacheProvider({
    partitionId: 'speech-v1',
    cacheStorage,
    cryptoImpl: webcrypto,
    origin: 'https://mobius.test',
    storageManager: {},
  })
  const events = []
  await runProvider(shell, { ...input, operation: 'read' }, { events })

  assert.equal(Buffer.from(events[0].value.bytes).toString(), 'shared voice')
  assert.equal(cacheStorage.values.has('mobius-device-assets-v1-speech-v1'), true)
  await assert.rejects(
    runProvider(shell, input),
    (error) => error.code === 'unavailable',
  )
})
