import { useEffect } from 'react'
import { subscribeToPushWithRetry } from '../lib/pushSubscription.js'

/**
 * Subscribes the browser to Web Push notifications after login.
 * Runs once per session — re-subscribes each time (subscriptions can
 * rotate), but only prompts for permission once.
 *
 * Push lives on its own service worker — see `lib/pushSubscription.js`. On a
 * first boot the worker installs for the first time WHILE this runs, so the
 * first subscribe can lose the race and reject; `subscribeToPushWithRetry`
 * retries in place so the grant takes effect without the user reloading.
 */
export default function usePushSubscription() {
  useEffect(() => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return
    // A denied permission can never be re-raised from here, so the whole
    // pipeline (worker install, key fetch, subscribe) would be wasted. Only
    // 'denied' short-circuits: 'default' is what raises the prompt.
    // `globalThis.` matters: a bare `Notification` is a ReferenceError, not
    // undefined, anywhere the API is absent.
    if (globalThis.Notification?.permission === 'denied') return
    // Push unsupported or the prompt refused — nothing to surface.
    subscribeToPushWithRetry().catch(() => {})
  }, [])
}
