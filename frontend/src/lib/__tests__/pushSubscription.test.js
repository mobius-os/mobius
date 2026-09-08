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
function fakeContainer({
  legacy = null,
  installing = false,
  updating = false,
  delayedUpdate = false,
} = {}) {
  const registered = []
  const refreshedWorker = (updating || delayedUpdate) ? fakeInstallingWorker() : null
  const updateListeners = []
  let resolveUpdatePublished = null
  const updatePublished = delayedUpdate
    ? new Promise((resolve) => { resolveUpdatePublished = resolve })
    : null
  const pushWorker = {
    scope: `${ORIGIN}${PUSH_SW_SCOPE}`,
    active: installing ? null : {},
    installing: installing ? fakeInstallingWorker() : null,
    waiting: null,
    updateCalls: 0,
    addEventListener(type, listener) {
      if (type === 'updatefound') updateListeners.push(listener)
    },
    removeEventListener(type, listener) {
      if (type !== 'updatefound') return
      const index = updateListeners.indexOf(listener)
      if (index !== -1) updateListeners.splice(index, 1)
    },
    async update() {
      this.updateCalls += 1
      if (refreshedWorker) {
        const publish = () => {
          this.installing = refreshedWorker
          updateListeners.slice().forEach((listener) => listener())
          resolveUpdatePublished?.()
        }
        if (delayedUpdate) setTimeout(publish, 0)
        else publish()
      }
      return this
    },
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
    refreshedWorker,
    updateListeners,
    updatePublished,
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
    subscribe: async (payload) => { push.sent.push(payload); return { ok: true } },
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
    { url: '/sw-push.js', scope: '/shell/push/', updateViaCache: 'none' },
  ])
  assert.equal(container.pushWorker.updateCalls, 1, 'an active worker is checked for updates')
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

test('an existing worker refresh waits for its replacement before subscribing',
  async () => {
    const container = fakeContainer({ updating: true })
    const worker = container.refreshedWorker
    const push = fakePush()

    const done = subscribeToPush({ container, push })
    await new Promise((resolve) => setImmediate(resolve))

    assert.equal(container.pushWorker.updateCalls, 1)
    assert.deepEqual(push.sent, [], 'does not subscribe through the stale active worker')
    assert.equal(worker.listeners.length, 1, 'waits for the refreshed worker')

    container.pushWorker.active = worker
    container.pushWorker.installing = null
    worker.settle('activated')
    await done

    assert.equal(push.sent.length, 1)
    assert.deepEqual(worker.listeners, [], 'the refresh listener is released')
    assert.deepEqual(container.updateListeners, [], 'the update listener is released')
  })

test('a replacement published after update resolves is still awaited', async () => {
  const container = fakeContainer({ delayedUpdate: true })
  const worker = container.refreshedWorker
  const push = fakePush()

  const done = subscribeToPush({ container, push })
  await container.updatePublished
  await new Promise((resolve) => setImmediate(resolve))

  assert.deepEqual(push.sent, [], 'does not miss the queued worker announcement')
  assert.equal(worker.listeners.length, 1, 'waits for the late-published worker')

  container.pushWorker.active = worker
  container.pushWorker.installing = null
  worker.settle('activated')
  await done

  assert.equal(push.sent.length, 1)
  assert.deepEqual(container.updateListeners, [], 'the update listener is released')
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

test('the retry gives up after the configured number of retries', async () => {
  let calls = 0
  const ok = await subscribeToPushWithRetry({
    subscribe: async () => { calls += 1; throw new Error('still installing') },
    retries: 2,
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

// A DISMISSED prompt leaves permission at 'default' (isDenied stays false), but
// the browser won't re-raise it — retrying just wastes time and can trip Chrome's
// auto-block heuristic, so a NotAllowedError stops the loop at once.
test('a dismissed prompt (NotAllowedError) stops without burning retries', async () => {
  let calls = 0
  const ok = await subscribeToPushWithRetry({
    subscribe: async () => {
      calls += 1
      const err = new Error('permission dismissed')
      err.name = 'NotAllowedError'
      throw err
    },
    isDenied: () => false, // dismiss, not block — permission is still 'default'
    sleep: async () => { throw new Error('must not sleep after a dismissed prompt') },
  })

  assert.equal(ok, false)
  assert.equal(calls, 1, 'a dismissed prompt is not retried')
})

test('a cancelled caller abandons the retry loop', async () => {
  let calls = 0
  let cancelled = false
  const ok = await subscribeToPushWithRetry({
    subscribe: async () => {
      calls += 1
      cancelled = true // e.g. the Shell unmounted during the first attempt
      throw new Error('still installing')
    },
    isCancelled: () => cancelled,
    sleep: async () => { throw new Error('must not sleep after cancellation') },
  })

  assert.equal(ok, false)
  assert.equal(calls, 1, 'no further attempts once cancelled')
})

// End-to-end through the REAL subscribeToPush: the first install goes redundant
// (subscribe throws), the retry re-registers onto an active worker and lands.
test('the retry drives the real subscribe from a redundant first install to success',
  async () => {
    const push = fakePush()
    // Settles redundant from INSIDE addEventListener (after the listener is
    // registered), so activatePushWorker's statechange promise actually resolves
    // — settling on a bare microtask would race ahead of the listener and hang.
    const redundantWorker = {
      state: 'installing',
      addEventListener: (_evt, fn) => {
        queueMicrotask(() => { redundantWorker.state = 'redundant'; fn() })
      },
      removeEventListener: () => {},
    }
    let registrations = 0
    const pushWorker = {
      scope: `${ORIGIN}${PUSH_SW_SCOPE}`,
      active: null,
      installing: redundantWorker,
      waiting: null,
      updateCalls: 0,
      addEventListener: () => {},
      removeEventListener: () => {},
      async update() {
        this.updateCalls += 1
        return this
      },
      pushManager: {
        subscribe: async (options) => {
          if (!pushWorker.active) throw new Error('InvalidStateError')
          pushWorker.subscribeOptions = options
          return fakeSubscription(PUSH_ENDPOINT)
        },
      },
    }
    const container = {
      register: async () => {
        registrations += 1
        if (registrations >= 2) { // the retry finds an active worker
          pushWorker.active = {}
          pushWorker.installing = null
        }
        return pushWorker
      },
      getRegistration: async () => undefined, // no legacy subscription
    }

    const ok = await subscribeToPushWithRetry({
      subscribe: () => subscribeToPush({ container, push }),
      sleep: async () => {},
    })

    assert.equal(ok, true)
    assert.equal(registrations, 2, 're-registered on the retry')
    assert.equal(pushWorker.updateCalls, 1, 'the retry checks the active worker')
    assert.equal(push.sent.length, 1, 'the subscription finally landed')
  })

// subscribeToPush must THROW on a cold-backend failure (not silently return),
// or the retry wrapper reports success while nothing was registered.
test('a non-ok VAPID key fetch throws so the retry can cover it', async () => {
  const container = fakeContainer()
  const push = fakePush()
  push.vapidKey = async () => ({ ok: false, status: 503 })

  await assert.rejects(subscribeToPush({ container, push }), /VAPID/)
})

test('a non-ok subscription POST throws so the retry can cover it', async () => {
  const container = fakeContainer()
  const push = fakePush()
  push.subscribe = async () => ({ ok: false, status: 500 })

  await assert.rejects(subscribeToPush({ container, push }), /registration failed/)
})
