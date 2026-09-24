import { after, before, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'

import {
  clearExpiredOwnerSession,
  clearQueryCache,
  getToken,
  setToken,
} from '../../api/client.js'
import {
  clearOutboxForTests,
  enqueueIntent,
  listIntents,
  outboxPrincipalKey,
} from '../../components/ChatView/chatOutbox.js'
import { clearExplicitOwnerSession } from '../explicitLogout.js'

const originalGlobals = {
  caches: globalThis.caches,
  indexedDB: globalThis.indexedDB,
  localStorage: globalThis.localStorage,
  sessionStorage: globalThis.sessionStorage,
  window: globalThis.window,
}
const localValues = new Map()
const sessionValues = new Map()

function storage(values) {
  return {
    get length() { return values.size },
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key) },
    setItem: (key, value) => { values.set(key, String(value)) },
  }
}

function ownerToken() {
  const payload = Buffer.from(JSON.stringify({ sub: 'owner', epoch: 3 }))
    .toString('base64url')
  return `stub.${payload}.stub`
}

function principalKey() {
  return outboxPrincipalKey(getToken())
}

async function queueOwnerIntent(cid) {
  await enqueueIntent({
    chatId: 'chat-1',
    cid,
    type: 'message',
    principalKey: principalKey(),
    body: { content: 'keep my accepted intent', cid },
  })
}

async function seedPrivateListing() {
  await new Promise((resolve, reject) => {
    const request = indexedDB.open('mobius-listings', 1)
    request.onupgradeneeded = () => request.result.createObjectStore('listings', { keyPath: 'key' })
    request.onerror = () => reject(request.error)
    request.onsuccess = () => {
      const db = request.result
      const tx = db.transaction('listings', 'readwrite')
      tx.objectStore('listings').put({ key: '7:owner:notes', entries: [{ name: 'private-title.json' }] })
      tx.oncomplete = () => { db.close(); resolve() }
      tx.onerror = () => reject(tx.error)
    }
  })
}

async function listingDatabaseExists() {
  const databases = typeof indexedDB.databases === 'function' ? await indexedDB.databases() : []
  return databases.some((database) => database.name === 'mobius-listings')
}

before(() => {
  globalThis.indexedDB = new IDBFactory()
  globalThis.localStorage = storage(localValues)
  globalThis.sessionStorage = storage(sessionValues)
  globalThis.window = globalThis
  globalThis.caches = { keys: async () => [], delete: async () => true }
})

beforeEach(async () => {
  localValues.clear()
  sessionValues.clear()
  setToken(ownerToken())
  await clearOutboxForTests()
})

after(() => {
  Object.assign(globalThis, originalGlobals)
})

test('credential expiry preserves principal-bound intent for the same owner', async () => {
  const owner = principalKey()
  await queueOwnerIntent('renew-me')

  await clearExpiredOwnerSession()

  assert.equal(getToken(), null)
  assert.equal(sessionStorage.getItem('auth_expired'), '1')
  assert.equal((await listIntents(owner)).length, 1)
})

test('explicit cleanup wipes principal-bound intent from the live outbox store', async () => {
  const owner = principalKey()
  await queueOwnerIntent('forget-me')

  await clearQueryCache()

  assert.equal((await listIntents(owner)).length, 0)
})

test('owner cleanup removes private cached directory metadata', async () => {
  await seedPrivateListing()
  assert.equal(await listingDatabaseExists(), true)

  await clearQueryCache()

  assert.equal(await listingDatabaseExists(), false)
})
test('explicit logout revokes copied handoffs before local owner state', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => { calls.push('stop') },
    revokeInstallHandoffs: async () => { calls.push('revoke') },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'credential', 'cache'])
})

test('failed handoff revocation never strands ordinary local logout', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => { calls.push('stop') },
    revokeInstallHandoffs: async () => {
      calls.push('revoke')
      throw new Error('offline')
    },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'credential', 'cache'])
})

test('failed local preparation stop still reaches server revocation', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => {
      calls.push('stop')
      throw new Error('cancel failed')
    },
    revokeInstallHandoffs: async () => { calls.push('revoke') },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'credential', 'cache'])
})
