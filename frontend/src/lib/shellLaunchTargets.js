// Routes installed-app launches and push-worker messages through one validated
// destination callback, leaving ordinary app-icon launches at the current view.
import { parseNotificationTarget } from './notificationTarget.js'

export function subscribeShellLaunchTargets(onOpen, {
  launchQueue = globalThis.window?.launchQueue,
  serviceWorker = globalThis.navigator?.serviceWorker,
} = {}) {
  let active = true
  const open = (raw) => {
    if (!active) return
    const target = parseNotificationTarget(raw)
    if (target) onOpen(target)
  }
  const onMessage = (event) => {
    if (event.data?.type === 'notification-click') open(event.data.target)
  }

  serviceWorker?.addEventListener('message', onMessage)
  // The manifest's focus-existing mode does NOT navigate a warm app. The
  // browser queues its target until this consumer is ready, including launches
  // received while the shell was loading. A plain /shell/ launch is a no-op.
  launchQueue?.setConsumer(({ targetURL }) => open(targetURL))

  return () => {
    active = false
    serviceWorker?.removeEventListener('message', onMessage)
    // LaunchQueue has no unsubscribe operation. An old callback must not act
    // on an unmounted shell; the next subscription replaces its consumer.
  }
}
