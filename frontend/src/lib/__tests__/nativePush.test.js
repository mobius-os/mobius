import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'

import {
  disableNativePush,
  enableNativePush,
  forgetNativePush,
  isNativePushEnabled,
  isThisPhone,
  registerNativePush,
  resetNativePushForTests,
} from '../nativePush.js'

const originalLocalStorage = globalThis.localStorage
const values = new Map()
const TOKEN_KEY = 'mobius.nativePush.token'
const ENABLED_KEY = 'mobius.nativePush.enabled'

function fakePush({ ok = true, forgetOk = true } = {}) {
  const calls = []
  return {
    calls,
    devices: {
      register: async (body) => {
        calls.push(['register', body])
        return ok
          ? { ok: true, status: 200 }
          : { ok: false, status: 409, json: async () => ({ detail: 'Link this Möbius first.' }) }
      },
      forget: async (body) => { calls.push(['forget', body]); return { ok: forgetOk, status: forgetOk ? 204 : 500 } },
    },
  }
}

// A fake app web view whose registerPush resolves to `result`.
function app(result) {
  const listeners = new Map()
  return {
    MobiusNative: { registerPush: async () => result },
    addEventListener(type, listener) { listeners.set(type, listener) },
    emit(type, detail) { listeners.get(type)?.({ detail }) },
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

beforeEach(() => {
  values.clear()
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, String(value)) },
    removeItem: (key) => { values.delete(key) },
  }
  resetNativePushForTests()
})

afterEach(() => {
  globalThis.localStorage = originalLocalStorage
})

test('outside the mobile app nothing is registered', async () => {
  const push = fakePush()
  await registerNativePush({ win: {}, push })
  assert.deepEqual(push.calls, [])
})

test('the shell never registers a phone the owner has not turned on', async () => {
  const push = fakePush()
  await registerNativePush({ win: app({ token: 'tok-1' }), push })
  assert.deepEqual(push.calls, [])
})

test('Turn on registers only the token and remembers the choice', async () => {
  const push = fakePush()
  const result = await enableNativePush({ win: app({ token: 'tok-1', platform: 'android' }), push })

  assert.deepEqual(result, { status: 'on' })
  assert.deepEqual(push.calls, [['register', { token: 'tok-1' }]])
  assert.equal(values.get(TOKEN_KEY), 'tok-1')
  assert.equal(isNativePushEnabled(), true)
})

test('once turned on, each shell start re-registers silently', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await enableNativePush({ win, push })
  resetNativePushForTests()

  await registerNativePush({ win, push })

  assert.deepEqual(push.calls.map(([kind]) => kind), ['register', 'register'])
})

test('denied permission leaves notifications off', async () => {
  const push = fakePush()
  assert.deepEqual(await enableNativePush({ win: app(null), push }), { status: 'denied' })
  assert.deepEqual(push.calls, [])
  assert.equal(isNativePushEnabled(), false)
})

test('a refused registration explains why and stays off', async () => {
  const push = fakePush({ ok: false })
  const result = await enableNativePush({ win: app({ token: 'tok-1' }), push })

  assert.deepEqual(result, { status: 'error', message: 'Link this Möbius first.' })
  assert.equal(isNativePushEnabled(), false)
  assert.equal(values.has(TOKEN_KEY), false)
})

test('a rotated token is registered only while turned on', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await enableNativePush({ win, push })

  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()
  assert.deepEqual(push.calls.slice(-2), [['register', { token: 'tok-2' }], ['forget', { token: 'tok-1' }]])

  await disableNativePush({ push })
  win.emit('mobius:push-token', { token: 'tok-3' })
  await settle()
  assert.deepEqual(push.calls.at(-1), ['forget', { token: 'tok-2' }])
})

test('Turn off forgets this phone and the shell stops registering it', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await enableNativePush({ win, push })

  assert.equal(await disableNativePush({ push }), null)
  await registerNativePush({ win, push })

  assert.deepEqual(push.calls.map(([kind]) => kind), ['register', 'forget'])
  assert.equal(values.has(TOKEN_KEY), false)
  assert.equal(values.has(ENABLED_KEY), false)
})

test('a failed Turn off says so and keeps the token to try again', async () => {
  const push = fakePush({ forgetOk: false })
  await enableNativePush({ win: app({ token: 'tok-1' }), push })

  assert.equal(typeof await disableNativePush({ push }), 'string')
  assert.equal(values.get(TOKEN_KEY), 'tok-1')
  assert.equal(isNativePushEnabled(), false)
})

test('sign-out forgets the phone and signing in again does not re-register it', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await enableNativePush({ win, push })

  await forgetNativePush({ push })
  await registerNativePush({ win, push })

  assert.deepEqual(push.calls.map(([kind]) => kind), ['register', 'forget'])
})

test('sign-out waits for a registration in flight and blocks later ones', async () => {
  const calls = []
  let finishRegister
  const push = {
    devices: {
      register: (body) => {
        calls.push(['register', body])
        return new Promise((resolve) => { finishRegister = () => resolve({ ok: true }) })
      },
      forget: async (body) => { calls.push(['forget', body]); return { ok: true } },
    },
  }
  const win = app({ token: 'tok-1' })
  values.set(ENABLED_KEY, '1')
  const registering = registerNativePush({ win, push })
  await settle()

  const forgetting = forgetNativePush({ push })
  finishRegister()
  await Promise.all([registering, forgetting])
  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  assert.deepEqual(calls, [['register', { token: 'tok-1' }], ['forget', { token: 'tok-1' }]])
})

test('Settings recognizes this phone by the end of its registered token', async () => {
  assert.equal(isThisPhone('ab12cd'), false)
  await enableNativePush({ win: app({ token: 'token-ab12cd' }), push: fakePush() })

  assert.equal(isThisPhone('ab12cd'), true)
  assert.equal(isThisPhone('zz99zz'), false)
  assert.equal(isThisPhone(''), false)
})

test('a new token for this phone replaces the old one instead of adding a second phone', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await enableNativePush({ win, push })

  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()
  await registerNativePush({ win: app({ token: 'tok-2' }), push })

  assert.deepEqual(push.calls, [
    ['register', { token: 'tok-1' }],
    ['register', { token: 'tok-2' }],
    ['forget', { token: 'tok-1' }],
    ['register', { token: 'tok-2' }],
  ])
})

test('signing out while Turn on waits for permission keeps notifications off', async () => {
  const push = fakePush()
  let grant
  const win = {
    MobiusNative: { registerPush: () => new Promise((resolve) => { grant = resolve }) },
    addEventListener() {},
  }
  const enabling = enableNativePush({ win, push })
  await settle()

  await forgetNativePush({ push })
  grant({ token: 'tok-1' })

  assert.deepEqual(await enabling, { status: 'cancelled' })
  assert.equal(isNativePushEnabled(), false)
  assert.deepEqual(push.calls, [])
})

test('an old token whose cleanup failed is retried until the server confirms', async () => {
  let forgetOk = false
  const calls = []
  const push = {
    devices: {
      register: async (body) => { calls.push(['register', body.token]); return { ok: true } },
      forget: async (body) => { calls.push(['forget', body.token]); return { ok: forgetOk } },
    },
  }
  const win = app({ token: 'tok-1' })
  await enableNativePush({ win, push })
  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  forgetOk = true
  assert.equal(await disableNativePush({ push }), null)

  assert.deepEqual(calls, [
    ['register', 'tok-1'], ['register', 'tok-2'], ['forget', 'tok-1'],
    ['forget', 'tok-1'], ['forget', 'tok-2'],
  ])
  assert.equal(values.has('mobius.nativePush.staleTokens'), false)
})
