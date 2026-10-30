import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createLocationTrackingProvider, LOCATION_TRACKING } from '../locationTrackingProvider.js'
import { builtInCapabilityProviders } from '../capabilityProviders.js'

function fakeController({ available = true, reported = null } = {}) {
  const calls = []
  let state = { available, enabled: false, status: null }
  let options = { background: true, interval: 10000, distance: 0 }
  return {
    calls,
    isNativeLocationAvailable: () => available,
    getLocationState: () => state,
    getTrackingOptions: () => options,
    async fetchReportedTracking() { calls.push('fetch-reported'); return reported },
    async startTracking() { calls.push('start'); state = { ...state, enabled: true, status: 'started' } },
    async stopTracking() { calls.push('stop'); state = { ...state, enabled: false, status: 'stopped' } },
    async setTrackingOptions(next) { calls.push(['set-options', next]); options = { ...options, ...next } },
  }
}

function invoke(provider, input) {
  return new Promise((resolve, reject) => {
    Promise.resolve(provider.open({
      input,
      declaration: { version: 1, limits: {} },
      channel: { result: resolve, error: reject, ready() {}, event() {} },
    })).catch(reject)
  })
}

test('location tracking is a built-in capability provider', () => {
  assert.equal(builtInCapabilityProviders()[LOCATION_TRACKING].version, 1)
})

test('start, set-options, and stop drive the phone controller and return its state', async () => {
  const controller = fakeController()
  const provider = createLocationTrackingProvider({ controller })

  assert.deepEqual(await invoke(provider, { operation: 'start' }), {
    available: true, enabled: true, status: 'started',
    options: { background: true, interval: 10000, distance: 0 },
  })
  const paced = await invoke(provider, { operation: 'set-options', options: { interval: 60000 } })
  assert.equal(paced.options.interval, 60000)
  const stopped = await invoke(provider, { operation: 'stop' })
  assert.equal(stopped.enabled, false)
  assert.equal(stopped.status, 'stopped')
  assert.deepEqual(controller.calls, ['start', ['set-options', { interval: 60000 }], 'stop'])
})

test('a fresh browser without the phone app reports what the phones last reported', async () => {
  const controller = fakeController({
    available: false, reported: { enabled: true, options: { interval: 60000 } },
  })
  const provider = createLocationTrackingProvider({ controller })

  assert.deepEqual(await invoke(provider, { operation: 'get' }), {
    available: false, enabled: true, status: null, options: { interval: 60000 },
  })
})

test('without the phone app an app can read the state but not change it', async () => {
  const controller = fakeController({ available: false })
  const provider = createLocationTrackingProvider({ controller })

  const state = await invoke(provider, { operation: 'get' })
  assert.equal(state.available, false)
  assert.equal(state.enabled, false)
  for (const operation of ['start', 'stop']) {
    await assert.rejects(invoke(provider, { operation }), (error) => error.code === 'unavailable')
  }
  await assert.rejects(
    invoke(provider, { operation: 'set-options', options: { interval: 5000 } }),
    (error) => error.code === 'unavailable',
  )
  assert.deepEqual(controller.calls, ['fetch-reported'])
})

test('unknown operations and malformed options are rejected before touching tracking', async () => {
  const controller = fakeController()
  const provider = createLocationTrackingProvider({ controller })

  await assert.rejects(invoke(provider, { operation: 'erase' }), TypeError)
  await assert.rejects(invoke(provider, { operation: 'set-options', options: [5000] }), TypeError)
  assert.deepEqual(controller.calls, [])
})
