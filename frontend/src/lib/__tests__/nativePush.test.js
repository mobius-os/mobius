import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'

import {
  forgetNativePush,
  registerNativePush,
  resetNativePushForTests,
} from '../nativePush.js'

const originalLocalStorage = globalThis.localStorage
const values = new Map()
const TOKEN_KEY = 'mobius.nativePush.token'

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

test('opening the app connects this phone without any switch', async () => {
  const push = fakePush()
  await registerNativePush({ win: app({ token: 'tok-1', platform: 'android' }), push })

  assert.deepEqual(push.calls, [['register', { token: 'tok-1' }]])
  assert.equal(values.get(TOKEN_KEY), 'tok-1')
})

test('each app open refreshes the registration', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await registerNativePush({ win, push })
  await registerNativePush({ win, push })

  assert.deepEqual(push.calls.map(([kind]) => kind), ['register', 'register'])
})

test('when the phone does not allow notifications, an earlier registration is forgotten', async () => {
  const push = fakePush()
  await registerNativePush({ win: app({ token: 'tok-1' }), push })

  await registerNativePush({ win: app(null), push })

  assert.deepEqual(push.calls.slice(1), [['forget', { token: 'tok-1' }]])
  assert.equal(values.has(TOKEN_KEY), false)
})

test('a phone that never allowed notifications sends nothing', async () => {
  const push = fakePush()
  await registerNativePush({ win: app(null), push })
  assert.deepEqual(push.calls, [])
})

test('a refused registration leaves this phone unrecorded', async () => {
  const push = fakePush({ ok: false })
  await registerNativePush({ win: app({ token: 'tok-1' }), push })

  assert.equal(values.has(TOKEN_KEY), false)
})

test('a new token for this phone replaces the old one instead of adding a second phone', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await registerNativePush({ win, push })

  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  assert.deepEqual(push.calls, [
    ['register', { token: 'tok-1' }],
    ['register', { token: 'tok-2' }],
    ['forget', { token: 'tok-1' }],
  ])
})

test('sign-out forgets the phone and a later rotation in the same page does not re-register it', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await registerNativePush({ win, push })

  await forgetNativePush({ push })
  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  assert.deepEqual(push.calls.map(([kind]) => kind), ['register', 'forget'])
})

test('signing in again in the app connects the phone again', async () => {
  const push = fakePush()
  const win = app({ token: 'tok-1' })
  await registerNativePush({ win, push })
  await forgetNativePush({ push })

  await registerNativePush({ win, push })

  assert.deepEqual(push.calls.map(([kind]) => kind), ['register', 'forget', 'register'])
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
  const registering = registerNativePush({ win, push })
  await settle()

  const forgetting = forgetNativePush({ push })
  finishRegister()
  await Promise.all([registering, forgetting])
  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  assert.deepEqual(calls, [['register', { token: 'tok-1' }], ['forget', { token: 'tok-1' }]])
})

test('signing out while the app is still answering keeps the phone unregistered', async () => {
  const push = fakePush()
  let answer
  const win = {
    MobiusNative: { registerPush: () => new Promise((resolve) => { answer = resolve }) },
    addEventListener() {},
  }
  const registering = registerNativePush({ win, push })
  await settle()

  await forgetNativePush({ push })
  answer({ token: 'tok-1' })
  await registering

  assert.deepEqual(push.calls, [])
  assert.equal(values.has(TOKEN_KEY), false)
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
  await registerNativePush({ win, push })
  win.emit('mobius:push-token', { token: 'tok-2' })
  await settle()

  forgetOk = true
  await forgetNativePush({ push })

  assert.deepEqual(calls, [
    ['register', 'tok-1'], ['register', 'tok-2'], ['forget', 'tok-1'],
    ['forget', 'tok-1'], ['forget', 'tok-2'],
  ])
  assert.equal(values.has('mobius.nativePush.staleTokens'), false)
})
