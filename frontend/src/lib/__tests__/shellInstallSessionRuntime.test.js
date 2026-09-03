import test from 'node:test'
import assert from 'node:assert/strict'

import {
  redeemInstalledShellSession,
  startShellInstallSessionLifecycle,
} from '../shellInstallSessionRuntime.js'


function eventTarget(extra = {}) {
  const listeners = new Map()
  return {
    ...extra,
    addEventListener(type, listener) {
      const current = listeners.get(type) || new Set()
      current.add(listener)
      listeners.set(type, current)
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener)
    },
    dispatch(type) {
      for (const listener of listeners.get(type) || []) listener()
    },
  }
}


test('redeems an installed shell before startup only when it lacks a session', async () => {
  const saved = []
  const result = await redeemInstalledShellSession({
    standaloneApp: () => false,
    standaloneShell: () => true,
    hasToken: () => false,
    redeem: async () => ({
      ok: true,
      json: async () => ({ access_token: 'installed-owner-session' }),
    }),
    saveToken: token => saved.push(token),
  })
  assert.equal(result, true)
  assert.deepEqual(saved, ['installed-owner-session'])

  let calls = 0
  assert.equal(await redeemInstalledShellSession({
    standaloneApp: () => false,
    standaloneShell: () => true,
    hasToken: () => true,
    redeem: async () => { calls += 1 },
  }), false)
  assert.equal(calls, 0)
})


test('browser lifecycle prepares on sign-in and aborts cleanly on teardown', () => {
  const windowTarget = eventTarget()
  const documentTarget = eventTarget({ visibilityState: 'visible' })
  let signedIn = false
  const calls = []
  const stop = startShellInstallSessionLifecycle({
    standaloneApp: () => false,
    standaloneShell: () => false,
    hasToken: () => signedIn,
    prepare: options => { calls.push(options); return Promise.resolve(true) },
    windowTarget,
    documentTarget,
  })
  assert.equal(calls.length, 0)

  signedIn = true
  windowTarget.dispatch('mobius:owner-token-changed')
  documentTarget.dispatch('visibilitychange')
  assert.deepEqual(calls.map(call => call.force), [true, true])
  assert.equal(calls[0].signal.aborted, false)

  stop()
  assert.equal(calls[0].signal.aborted, true)
  windowTarget.dispatch('pageshow')
  assert.equal(calls.length, 2)
})
