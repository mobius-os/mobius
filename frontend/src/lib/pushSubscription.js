import { api } from '../api/client.js'

/**
 * Web Push subscription lifecycle, kept out of React so it can be exercised
 * directly. `public/sw-push.js` explains why push has its own worker and why
 * its scope has to look the way it does.
 */
export const PUSH_SW_SCOPE = '/shell/push/'
const PUSH_SW_URL = '/sw-push.js'

// Scopes an earlier release subscribed on. This is an ALLOWLIST on purpose:
// "retire every registration that isn't ours" would silently unsubscribe a
// standalone mini-app's own push worker the moment those exist, and the loss
// is invisible until a notification fails to arrive on someone's phone.
// Removable once no install can still be running the pre-/shell/push/ shell.
const LEGACY_PUSH_SCOPES = ['/']

/** Register the push worker and resolve once it has an active worker. */
async function activatePushWorker(container) {
  const registration = await container.register(
    PUSH_SW_URL, { scope: PUSH_SW_SCOPE },
  )
  if (registration.active) return registration
  const worker = registration.installing
  if (!worker) return registration
  // register() resolves while the worker is still installing, and
  // pushManager.subscribe() needs an active one. `redundant` resolves too, so
  // a failed install surfaces as a subscribe error instead of hanging here
  // forever on a promise nobody can settle.
  await new Promise((resolve) => {
    const onChange = () => {
      if (worker.state === 'activated' || worker.state === 'redundant') {
        worker.removeEventListener('statechange', onChange)
        resolve()
      }
    }
    worker.addEventListener('statechange', onChange)
  })
  return registration
}

/**
 * Retire a subscription an older release left on the caching worker.
 *
 * That worker no longer handles `push`, so a send to its endpoint would trip
 * the browser's userVisibleOnly fallback and show a generic "site updated in
 * the background" notification. Drop it server-side first, then locally, so a
 * failure can't strand an endpoint in the database still receiving sends. The
 * registration itself is left alone — it is the shell's cache.
 */
async function retireLegacySubscriptions(container, push) {
  for (const scope of LEGACY_PUSH_SCOPES) {
    const registration = await container.getRegistration(scope)
    if (!registration) continue
    let stale
    try {
      stale = await registration.pushManager.getSubscription()
    } catch {
      continue // No push support here, or the permission was revoked.
    }
    if (!stale) continue
    await push.unsubscribe({ endpoint: stale.endpoint })
    await stale.unsubscribe()
  }
}

/** base64url VAPID key → the Uint8Array `subscribe()` wants. */
function applicationServerKey(publicKey) {
  const padding = '='.repeat((4 - publicKey.length % 4) % 4)
  const raw = atob(publicKey.replace(/-/g, '+').replace(/_/g, '/') + padding)
  const key = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) key[i] = raw.charCodeAt(i)
  return key
}

/**
 * Subscribe this browser to Web Push and hand the subscription to the server.
 * Safe to call on every session — subscriptions rotate, and the browser only
 * prompts for permission once.
 */
export async function subscribeToPush({
  container = navigator.serviceWorker,
  push = api.push,
} = {}) {
  // Independent: the worker's first install is a real fetch, and holding the
  // key request behind it costs a round trip on every fresh install.
  const [registration, res] = await Promise.all([
    activatePushWorker(container),
    push.vapidKey(),
  ])
  if (!res.ok) return

  const { publicKey } = await res.json()
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: applicationServerKey(publicKey),
  })

  const { endpoint, keys } = subscription.toJSON()
  await push.subscribe({ endpoint, keys })

  // Only once the replacement is registered server-side.
  await retireLegacySubscriptions(container, push)
}

/**
 * Subscribe, retrying a few times so a first-boot install lands without a reload.
 *
 * On the very first visit the push worker installs for the first time WHILE this
 * runs: `register()` resolves on a worker that can still go `redundant`, so
 * `subscribeToPush()` rejects (see its test "a failed install settles instead of
 * hanging forever"). A single attempt then swallows that and the subscription
 * only registers when the user happens to reload — the "I allowed notifications
 * but had to reload for it to take effect" report. A retry re-registers the
 * worker (idempotent once active) and subscribes in place instead.
 *
 * A DENIED prompt can never be re-raised, so we stop the moment permission is
 * denied rather than burning the remaining attempts. Deps are injected so the
 * retry policy is unit-testable without real timers or a service worker.
 */
export async function subscribeToPushWithRetry({
  subscribe = subscribeToPush,
  attempts = 3,
  backoffMs = (attempt) => 1000 * 2 ** attempt,
  isDenied = () => globalThis.Notification?.permission === 'denied',
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
} = {}) {
  for (let attempt = 0; attempt <= attempts; attempt++) {
    if (isDenied()) return false
    try {
      await subscribe()
      return true
    } catch {
      // The last attempt, or a decision the user won't be asked again, is
      // terminal — nothing left to retry.
      if (attempt === attempts || isDenied()) return false
      await sleep(backoffMs(attempt))
    }
  }
  return false
}
