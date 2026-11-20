// Settings › Environment: waiting for Möbius to come back after a side switch.
import { afterEach, beforeEach, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import { GIVE_UP_AFTER, RECHECK_MS, useSideSwitchWatch } from '../useSideSwitchWatch.js'

const saved = {}
let asked
let reloads
let serverSide

beforeEach(() => {
  for (const key of ['window', 'fetch']) saved[key] = Object.getOwnPropertyDescriptor(globalThis, key)
  asked = 0
  reloads = 0
  serverSide = 'production'
  globalThis.window = Object.assign(new EventTarget(), { location: { reload: () => { reloads += 1 } } })
  globalThis.fetch = async () => {
    asked += 1
    return new Response(JSON.stringify({ available: true, side: serverSide }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })
  }
  mock.timers.enable({ apis: ['setInterval'] })
})
afterEach(() => {
  mock.timers.reset()
  for (const [key, descriptor] of Object.entries(saved)) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor)
    else delete globalThis[key]
  }
})

const settle = async () => { for (let i = 0; i < 5; i += 1) await new Promise(resolve => setImmediate(resolve)) }

test('nothing is asked before a side has been chosen', async () => {
  const hook = renderHook(() => useSideSwitchWatch(null))
  mock.timers.tick(RECHECK_MS * 5)
  await settle()
  assert.equal(asked, 0)
  assert.equal(hook.result.current, false)
  hook.unmount()
})

test('the page reloads once the server answers on the chosen side', async () => {
  const hook = renderHook(() => useSideSwitchWatch('staging'))
  mock.timers.tick(RECHECK_MS)
  await settle()
  assert.equal(reloads, 0)
  serverSide = 'staging'
  mock.timers.tick(RECHECK_MS)
  await settle()
  assert.equal(reloads, 1)
  hook.unmount()
})

test('giving up stops the polling for good', async () => {
  const hook = renderHook(() => useSideSwitchWatch('staging'))
  for (let i = 0; i < GIVE_UP_AFTER; i += 1) mock.timers.tick(RECHECK_MS)
  await settle()
  assert.equal(hook.result.current, false)
  mock.timers.tick(RECHECK_MS)
  await settle()
  assert.equal(hook.result.current, true)
  const askedAtGiveUp = asked
  mock.timers.tick(RECHECK_MS * 10)
  await settle()
  assert.equal(asked, askedAtGiveUp)
  hook.unmount()
})
