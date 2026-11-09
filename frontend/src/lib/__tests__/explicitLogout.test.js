import { after, before, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'

import {
  clearExpiredOwnerSession,
  clearQueryCache,
  endExpiredOwnerSession,
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
import { resetNativeSessionForTests } from '../nativeSession.js'

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
    forgetPushDevice: async () => { calls.push('push') },
    stopLocationTracking: async () => { calls.push('location') },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'push', 'location', 'credential', 'cache'])
})

test('failed handoff revocation or push sign-out never strands ordinary local logout', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => { calls.push('stop') },
    revokeInstallHandoffs: async () => {
      calls.push('revoke')
      throw new Error('offline')
    },
    forgetPushDevice: async () => {
      calls.push('push')
      throw new Error('offline')
    },
    stopLocationTracking: async () => {
      calls.push('location')
      throw new Error('offline')
    },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'push', 'location', 'credential', 'cache'])
})

test('failed local preparation stop still reaches server revocation', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => {
      calls.push('stop')
      throw new Error('cancel failed')
    },
    revokeInstallHandoffs: async () => { calls.push('revoke') },
    forgetPushDevice: async () => { calls.push('push') },
    stopLocationTracking: async () => { calls.push('location') },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'push', 'location', 'credential', 'cache'])
})

test('explicit logout records that it was deliberate for the sign-in screen', async () => {
  localStorage.removeItem('mobius_signed_out')

  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => {},
    revokeInstallHandoffs: async () => {},
    dropCredential: () => {},
    clearOwnerCache: async () => {},
  })

  assert.equal(localStorage.getItem('mobius_signed_out'), '1')
})

function credentialExpiring(offsetMs) {
  const exp = Math.floor((Date.now() + offsetMs) / 1000)
  const payload = Buffer.from(JSON.stringify({ sub: 'owner', epoch: 3, exp })).toString('base64url')
  return `stub.${payload}.stub`
}

async function endExpiryWith(MobiusNative, credential) {
  const reloads = []
  const originalLocation = globalThis.location
  const originalSetTimeout = globalThis.setTimeout
  resetNativeSessionForTests()
  globalThis.MobiusNative = MobiusNative
  globalThis.location = { reload: () => reloads.push('reload') }
  globalThis.setTimeout = (fn) => { fn(); return 0 }
  try {
    await endExpiredOwnerSession({ credential })
  } finally {
    delete globalThis.MobiusNative
    globalThis.location = originalLocation
    globalThis.setTimeout = originalSetTimeout
    resetNativeSessionForTests()
  }
  return reloads
}

test('a cancelled session in the phone app hands over to the app after cleanup', async () => {
  let tokenWhenTold = 'not told'
  const reloads = await endExpiryWith({
    signOut: async () => { tokenWhenTold = getToken(); return null },
  }, credentialExpiring(24 * 3600 * 1000))
  assert.equal(tokenWhenTold, null, 'the workspace session is retired before the app is told')
  assert.deepEqual(reloads, [], 'the app closes the page; the shell must not reload it')
})

test('a timed-out session in the phone app keeps the app signed in and reloads', async () => {
  // The app's own sign-in outlives the workspace credential; signing it out
  // would only add a second sign-in.
  let told = false
  const reloads = await endExpiryWith({
    signOut: async () => { told = true; return null },
  }, credentialExpiring(-60 * 1000))
  assert.equal(told, false)
  assert.deepEqual(reloads, ['reload'])
})

test('an expired session in a normal browser reloads into the sign-in screen', async () => {
  const reloads = await endExpiryWith(undefined, credentialExpiring(24 * 3600 * 1000))
  assert.equal(getToken(), null)
  assert.deepEqual(reloads, ['reload'])
})
