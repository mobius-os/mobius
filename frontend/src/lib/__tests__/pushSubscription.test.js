import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  PUSH_SW_SCOPE,
  subscribeToPush,
  subscribeToPushWithRetry,
} from '../pushSubscription.js'

// Mirrors the shell PWA's manifest scope (frontend/public/manifest.webmanifest).
// Android hands a push to the installed Möbius app only when the service
// worker's scope resolves to that WebAPK, so a push scope outside this makes
// every notification a plain Chrome notification whose tap opens Chrome. That
// failure is invisible on desktop and in headless runs. The build-time gate in
// scripts/check-offline-build.mjs checks the real manifest; this pins the
// relationship for the unit suite.
const SHELL_MANIFEST_SCOPE = '/shell/'

const ORIGIN = 'https://mobius.test'
const PUSH_ENDPOINT = `${ORIGIN}/endpoint/push`
const LEGACY_ENDPOINT = `${ORIGIN}/endpoint/legacy`

function fakeSubscription(endpoint) {
  return {
    endpoint,
    unsubscribed: false,
    toJSON: () => ({ endpoint, keys: { p256dh: 'p', auth: 'a' } }),
    async unsubscribe() { this.unsubscribed = true },
  }
}

/**
 * @param legacy - what `getRegistration('/')` resolves to: a subscription to
 *   retire, `null` for a caching worker that never held one, `'throws'` for a
 *   registration whose pushManager is unusable, or `'missing'` for none at all.
 */
function fakeContainer({ legacy = null, installing = false } = {}) {
  const registered = []
  const pushWorker = {
    scope: `${ORIGIN}${PUSH_SW_SCOPE}`,
    active: installing ? null : {},
    installing: installing ? fakeInstallingWorker() : null,
    pushManager: {
      subscribe: async (options) => {
        // The browser rejects with InvalidStateError when the registration has
        // no active worker; the fake has to model that or the wait for
        // activation looks unnecessary.
        if (!pushWorker.active) throw new Error('InvalidStateError')
        pushWorker.subscribeOptions = options
        return fakeSubscription(PUSH_ENDPOINT)
      },
    },
  }
  return {
    registered,
    pushWorker,
    register: async (url, options) => {
      registered.push({ url, ...options })
      return pushWorker
    },
    getRegistration: async (scope) => {
      if (scope !== '/' || legacy === 'missing') return undefined
      return {
        scope: `${ORIGIN}/`,
        pushManager: {
          getSubscription: async () => {
            if (legacy === 'throws') throw new Error('permission revoked')
            return legacy
          },
        },
      }
    },
  }
}

function fakeInstallingWorker() {
  const worker = { state: 'installing', listeners: [] }
  worker.addEventListener = (_, fn) => worker.listeners.push(fn)
  worker.removeEventListener = (_, fn) => {
    worker.listeners = worker.listeners.filter(l => l !== fn)
  }
  worker.settle = (state) => {
    worker.state = state
    for (const fn of [...worker.listeners]) fn()
  }
  return worker
}

function fakePush() {
  const push = {
    sent: [],
    removed: [],
    vapidKey: async () => ({ ok: true, json: async () => ({ publicKey: 'QUJD' }) }),
    subscribe: async (payload) => { push.sent.push(payload) },
    unsubscribe: async (payload) => { push.removed.push(payload) },
  }
  return push
}

test('the push worker is registered inside the shell PWA scope', async () => {
  const container = fakeContainer()
  await subscribeToPush({ container, push: fakePush() })

  // The URL is a three-way contract with vite.config.js globIgnores and the
  // backend's worker delivery contract, so pin the literals, not the constants.
  assert.deepEqual(container.registered, [
    { url: '/sw-push.js', scope: '/shell/push/' },
  ])
  assert.ok(
    PUSH_SW_SCOPE.startsWith(SHELL_MANIFEST_SCOPE),
    `${PUSH_SW_SCOPE} must sit inside the shell scope ${SHELL_MANIFEST_SCOPE}`,
  )
  // A worker registered AT the manifest scope would out-match the `/`-scoped
  // caching worker for the shell's own pages, take control of them, and
  // disable the shell precache. The extra segment is what prevents that.
  assert.notEqual(PUSH_SW_SCOPE, SHELL_MANIFEST_SCOPE)
})

test('the subscription is created on the push worker and sent to the server',
  async () => {
    const container = fakeContainer()
    const push = fakePush()

    await subscribeToPush({ container, push })

    assert.equal(container.pushWorker.subscribeOptions.userVisibleOnly, true)
    assert.deepEqual(push.sent, [{
      endpoint: PUSH_ENDPOINT,
      keys: { p256dh: 'p', auth: 'a' },
    }])
  })

test('subscribing waits for a freshly installed worker to activate',
  async () => {
    // register() resolves while the worker is still installing, and
    // pushManager.subscribe() needs an active one.
    const container = fakeContainer({ installing: true })
    const worker = container.pushWorker.installing
    const push = fakePush()

    const done = subscribeToPush({ container, push })
    await Promise.resolve()
    assert.deepEqual(push.sent, [], 'does not subscribe while installing')

    container.pushWorker.active = {}
    worker.settle('activated')
    await done

    assert.equal(push.sent.length, 1)
    assert.deepEqual(worker.listeners, [], 'the statechange listener is released')
  })

test('a failed install settles instead of hanging forever', async () => {
  const container = fakeContainer({ installing: true })
  const worker = container.pushWorker.installing

  const done = subscribeToPush({ container, push: fakePush() })
  await Promise.resolve()
  worker.settle('redundant')

  // No active worker, so subscribe() rejects — the caller's catch surfaces it.
  // The point is that this resolves at all rather than stranding the promise.
  await assert.rejects(done)
})

test('a subscription left on the caching worker is retired', async () => {
  // sw.js no longer handles `push`, so a send to its endpoint would trip the
  // browser's userVisibleOnly fallback ("site updated in the background").
  const stale = fakeSubscription(LEGACY_ENDPOINT)
  const container = fakeContainer({ legacy: stale })
  const push = fakePush()

  await subscribeToPush({ container, push })

  assert.deepEqual(push.removed, [{ endpoint: LEGACY_ENDPOINT }])
  assert.equal(stale.unsubscribed, true)
})

test('only the enumerated legacy scopes are retired', async () => {
  // Retirement is an allowlist: a future standalone mini-app push worker must
  // not be unsubscribed just because it is not the shell's.
  const container = fakeContainer({ legacy: 'missing' })
  const push = fakePush()
  const seen = []
  const getRegistration = container.getRegistration
  container.getRegistration = async (scope) => {
    seen.push(scope)
    return getRegistration(scope)
  }

  await subscribeToPush({ container, push })

  assert.deepEqual(seen, ['/'])
  assert.deepEqual(push.removed, [])
})

test('an unusable legacy pushManager is skipped, not fatal', async () => {
  const container = fakeContainer({ legacy: 'throws' })
  const push = fakePush()

  await subscribeToPush({ container, push })

  assert.deepEqual(push.removed, [])
  assert.equal(push.sent.length, 1, 'the new subscription still lands')
})

// A first-boot install can race the very first worker activation and reject the
// first subscribe; the retry is what lets the grant take effect without a reload.
test('a first-boot install failure is retried until it lands', async () => {
  let calls = 0
  const slept = []
  const ok = await subscribeToPushWithRetry({
    subscribe: async () => {
      calls += 1
      if (calls < 2) throw new Error('InvalidStateError') // redundant first install
    },
    sleep: async (ms) => { slept.push(ms) },
  })

  assert.equal(ok, true)
  assert.equal(calls, 2, 'retried after the racing first install failed')
  assert.deepEqual(slept, [1000], 'backed off once before the retry')
})

test('a denied permission stops the retry loop immediately', async () => {
  let calls = 0
  let denied = false
  const ok = await subscribeToPushWithRetry({
    subscribe: async () => {
      calls += 1
      denied = true // the user just dismissed the prompt as "block"
      throw new Error('NotAllowedError')
    },
    isDenied: () => denied,
    sleep: async () => { throw new Error('must not sleep after a denial') },
  })

  assert.equal(ok, false)
  assert.equal(calls, 1, 'a denied prompt is not re-raised')
})

test('the retry gives up after the configured attempts', async () => {
  let calls = 0
  const ok = await subscribeToPushWithRetry({
    subscribe: async () => { calls += 1; throw new Error('still installing') },
    attempts: 2,
    sleep: async () => {},
  })

  assert.equal(ok, false)
  assert.equal(calls, 3, 'initial attempt plus two retries')
})

test('a subscribe that never had to be asked twice does not retry', async () => {
  let calls = 0
  const ok = await subscribeToPushWithRetry({
    subscribe: async () => { calls += 1 },
    sleep: async () => { throw new Error('must not sleep on success') },
  })

  assert.equal(ok, true)
  assert.equal(calls, 1)
})
