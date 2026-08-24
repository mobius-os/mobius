/**
 * Möbius push worker — registered at scope `/shell/push/`.
 *
 * WHY THIS IS A SEPARATE WORKER FROM `sw.js`
 * ------------------------------------------
 * On Android, Chrome decides WHICH INSTALLED APP receives a web push by
 * resolving the *service worker's scope URL* against the intent filters of
 * installed WebAPKs — `NotificationPlatformBridge.getWebApkPackage(scopeUrl)`
 * → `WebApkValidator.queryFirstWebApkPackage(context, scopeUrl)`. A WebAPK's
 * intent filter carries its manifest `scope` as an Android `pathPrefix`. When
 * a WebAPK matches, Chrome hands the notification to it (`WebApkServiceClient`)
 * and bakes the package name into the click PendingIntent, so tapping launches
 * that app. When nothing matches, Chrome displays and owns the notification
 * itself, and a tap opens Chrome.
 *
 * Möbius's shell is installed with `scope: "/shell/"`, but the caching worker
 * (`sw.js`) is deliberately registered at `/` — it also has to serve the
 * standalone mini-app pages under `/apps/<slug>/`, which are separate PWAs on
 * the same origin. `/` is NOT inside `/shell/`, so no WebAPK ever matched:
 * every notification was a plain Chrome site notification and every tap landed
 * in Chrome instead of Möbius.
 *
 * This worker exists solely to own the push subscription from a scope that is
 * inside the shell WebAPK's scope. `/shell/push/` holds no documents — the
 * backend 404s it and `swNavigationPolicy` keeps the shell off it — so this
 * registration controls ZERO pages, which is the point. A worker registered at
 * `/shell/` itself would be a narrower match than `sw.js` for the shell's own
 * pages, take control of them, and silently disable the shell's precache and
 * offline behaviour.
 *
 * Served verbatim from `public/` (no bundler), so it must stay dependency-free
 * and valid as a classic worker script.
 */

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()))

self.addEventListener('push', (e) => {
  if (!e.data) return
  const data = e.data.json()
  const options = {
    body: data.body || '',
    icon: data.icon || '/icons/icon-192.png',
    // Android renders `badge` as the tiny monochrome status-bar glyph.
    // Keep it separate from the full-colour notification card icon.
    badge: '/icons/notification-badge.png',
    data: { target: data.target || '/', actions: data.actions },
    actions: (data.actions || []).slice(0, 2).map(a => ({
      action: a.action,
      title: a.title,
    })),
  }
  e.waitUntil(self.registration.showNotification(data.title, options))
})

// Whitelist notification targets to same-origin chat/app paths so a
// malicious payload (server compromise, MITM of an unencrypted push)
// can't steer openWindow() or postMessage to an arbitrary URL.
//
// COLD-START SCOPE: the canonical target is `/shell/?app=<id>` (and
// `/shell/?chat=<id>`), which is INSIDE the PWA manifest scope (`/shell/`).
// A cold tap (app closed/backgrounded) does `clients.openWindow(target)`;
// only a target inside scope reopens the installed standalone PWA — an
// out-of-scope form opens a plain browser tab instead. The retired
// `/app/<id>` and `/chat/<id>` legacy forms are no longer accepted (the last
// one on prod predates this by weeks); they now fall through to root. We
// preserve the query string so the page-side parser (Shell onSwMessage,
// useNavigation deepLink) can read `?app=`/`?chat=`.
function _safeTarget(raw) {
  if (typeof raw !== 'string' || !raw) return '/'
  let path = raw
  let search = ''
  try {
    if (/^https?:\/\//i.test(raw)) {
      const u = new URL(raw)
      if (u.origin !== self.location.origin) return '/'
      path = u.pathname
      search = u.search
    } else {
      const q = raw.indexOf('?')
      if (q !== -1) {
        path = raw.slice(0, q)
        search = raw.slice(q)
      }
    }
  } catch { return '/' }
  // In-scope shell deep-link: /shell/ or /shell with an ?app=/?chat= query.
  if (/^\/shell\/?$/.test(path)) {
    try {
      const params = new URLSearchParams(search)
      const app = params.get('app')
      const chat = params.get('chat')
      // An app deep-link may carry a one-shot intent naming WHICH item to
      // open (e.g. `artifact:tip-calculator-7f3a`). Dropping it here landed
      // the tap on the app's index instead of the item the notification was
      // about. Same conservative charset as the ids, plus the ':' and '.'
      // that namespace an intent's target.
      const intent = params.get('intent')
      if (app && /^[A-Za-z0-9_-]+$/.test(app)) {
        return (intent && /^[A-Za-z0-9_.:-]{1,128}$/.test(intent))
          ? `/shell/?app=${app}&intent=${encodeURIComponent(intent)}`
          : `/shell/?app=${app}`
      }
      if (chat && /^[A-Za-z0-9_-]+$/.test(chat)) return `/shell/?chat=${chat}`
    } catch { /* fall through */ }
    return '/shell/'
  }
  // Root is the only remaining out-of-scope target we accept; every other
  // form (including the retired /app/<id> and /chat/<id> notification
  // targets) falls through to root.
  if (path === '/') return path
  return '/'
}

self.addEventListener('notificationclick', (e) => {
  e.notification.close()
  const data = e.notification.data || {}
  let target = data.target || '/'

  if (e.action && data.actions) {
    const match = data.actions.find(a => a.action === e.action)
    if (match && match.target) target = match.target
  }
  target = _safeTarget(target)

  e.waitUntil((async () => {
    // `includeUncontrolled` is load-bearing here: this worker deliberately
    // controls no documents, so every shell window is an uncontrolled client
    // of it. matchAll is origin-scoped, not path-scoped, so the shell's own
    // windows are still visible to us.
    const windowClients = await self.clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    })
    const focusable = windowClients.filter(c => 'focus' in c)
    // Prefer a client the user is currently looking at — focusing a
    // hidden/background tab would steer the message away from the
    // window they're actually using. Fall back to the first match
    // if nothing is visible.
    const visible = focusable.find(c => c.visibilityState === 'visible')
    const target_client = visible || focusable[0]
    if (target_client) {
      // For shell deep-links, navigate the existing client instead of only
      // postMessaging it. The message path is fast when the current Shell
      // listener is alive, but installed PWAs can have a stale/booting page
      // after a service-worker update; navigation gives the browser a durable
      // URL to load so a "Open <app>" action does not focus Mobius and then
      // appear to do nothing.
      if (/^\/shell\/?\?/.test(target) && 'navigate' in target_client) {
        try {
          const navigated = await target_client.navigate(target)
          await (navigated || target_client).focus()
          return
        } catch {
          // Fall back to focus + postMessage below.
        }
      }
      // Focus BEFORE postMessage so the message lands on the window
      // the user will end up on. If focus moves the active document
      // mid-handler, postMessage on the un-focused one can race.
      await target_client.focus()
      target_client.postMessage({ type: 'notification-click', target })
      return
    }
    if (self.clients.openWindow) return self.clients.openWindow(target)
  })())
})
