// Service worker: PWA install + Web Push + runtime asset caching.
//
// Cache strategy by URL:
//   /vendor/*                   cache-first    (immutable bundled libs)
//   /assets/*                   cache-first    (Vite-hashed shell assets)
//   /api/proxy?url=*.{img/font} SWR            (cacheable static assets only)
//   esm.sh/*                    cache-first    (versioned URLs are immutable)
// Everything else (HTML, /api/*) goes straight to the network.
//
// `/api/apps/{id}/{frame,module}` are INTENTIONALLY NOT intercepted by
// the SW. The server returns an ETag derived from `app.updated_at` and
// `Cache-Control: no-cache`, so the browser's own HTTP cache handles
// revalidation via `If-None-Match` automatically. SW interception
// would shortcut that with cache-first, which is exactly the
// "spinner-forever" failure mode we previously hit — the SW kept
// returning a stale broken module across reloads because the
// `?v=` cache key reset to 0 on every Shell mount.
//
// Bumping VERSION purges all old `mobius-*` caches on activate.
// v6: retire the `mobius-apps-vN` cache (the routes that used it are
// no longer intercepted). The activate-purge will reclaim the disk
// space from any pre-v6 instances.
const VERSION = 'v6'
const CACHES = {
  vendor: `mobius-vendor-${VERSION}`,
  assets: `mobius-assets-${VERSION}`,
  proxy: `mobius-proxy-${VERSION}`,
  esm: `mobius-esm-${VERSION}`,
}
const KNOWN_CACHE_NAMES = new Set(Object.values(CACHES))

// File-extension allowlist for /api/proxy SWR — anything else (JSON
// APIs that change frequently, like ISS positions) bypasses cache so
// the live data isn't frozen.
const CACHEABLE_PROXY_EXT =
  /\.(jpg|jpeg|png|gif|webp|svg|ico|woff2?|ttf|otf|eot|hdr|exr|mp3|mp4|webm|ogg|wav)(\?|$)/i

self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys()
    await Promise.all(
      keys
        .filter(k => k.startsWith('mobius-') && !KNOWN_CACHE_NAMES.has(k))
        .map(k => caches.delete(k))
    )
    await self.clients.claim()
  })())
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  const path = url.pathname

  if (url.origin === self.location.origin) {
    if (path.startsWith('/vendor/')) {
      event.respondWith(cacheFirst(req, CACHES.vendor))
      return
    }
    if (path.startsWith('/assets/')) {
      event.respondWith(cacheFirst(req, CACHES.assets))
      return
    }
    // /api/apps/{id}/frame and /api/apps/{id}/module deliberately
    // fall through to the network — the browser handles freshness
    // via the ETag the server returns. SW interception used to
    // cache-first these and held stale modules across reloads.
    if (path === '/api/proxy') {
      const upstream = url.searchParams.get('url') || ''
      if (CACHEABLE_PROXY_EXT.test(upstream)) {
        event.respondWith(staleWhileRevalidate(req, CACHES.proxy))
      }
      return
    }
    return
  }

  if (url.hostname === 'esm.sh') {
    event.respondWith(cacheFirst(req, CACHES.esm))
  }
})

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName)
  const cached = await cache.match(req)
  if (cached) return cached
  try {
    const res = await fetch(req)
    if (res.ok) cache.put(req, res.clone()).catch(() => {})
    return res
  } catch (err) {
    return cached || Response.error()
  }
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName)
  const cached = await cache.match(req)
  const network = fetch(req).then(res => {
    if (res.ok) cache.put(req, res.clone()).catch(() => {})
    return res
  }).catch(() => cached)
  return cached || network
}

// Web Push: show notification when a push arrives.
self.addEventListener('push', (e) => {
  if (!e.data) return
  const data = e.data.json()
  const options = {
    body: data.body || '',
    icon: data.icon || '/moebius.png',
    badge: '/moebius.png',
    data: { target: data.target || '/', actions: data.actions },
    actions: (data.actions || []).slice(0, 2).map(a => ({
      action: a.action,
      title: a.title,
    })),
  }
  e.waitUntil(self.registration.showNotification(data.title, options))
})

// Notification tap: deep-link into the PWA. Earlier versions of this
// file were truncated mid-handler — the whole sw.js failed `node
// --check` with a SyntaxError, so the browser couldn't register the
// service worker on installed PWAs. Net effect was: no offline cache,
// no push click handling, no app-frame caching, no logout cache-purge.
// Keep the handler complete; run `node --check frontend/public/sw.js`
// after any edit here.
// Whitelists notification targets to same-origin chat/app paths to
// prevent a malicious notification payload (server compromise, MITM
// of an unencrypted push) from steering us to an arbitrary URL via
// openWindow() or driving Shell.jsx's navTo with a bogus target.
function _safeTarget(raw) {
  if (typeof raw !== 'string' || !raw) return '/'
  let path = raw
  try {
    if (/^https?:\/\//.test(raw)) {
      const u = new URL(raw)
      if (u.origin !== self.location.origin) return '/'
      path = u.pathname
    }
  } catch { return '/' }
  if (path === '/' || /^\/chat\/[^/]+$/.test(path)
      || /^\/app\/[^/]+$/.test(path)) {
    return path
  }
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
    const windowClients = await clients.matchAll({
      type: 'window',
      includeUncontrolled: true,
    })
    const focusable = windowClients.filter(c => 'focus' in c)
    // Prefer a client the user is currently looking at — focusing a
    // hidden/background tab would steer the message away from the
    // window they're actually using. Fall back to the first match if
    // nothing is visible.
    const visible = focusable.find(c => c.visibilityState === 'visible')
    const target_client = visible || focusable[0]
    if (target_client) {
      // Focus BEFORE postMessage so the message lands on the window
      // the user will end up on. If focus moves the active document
      // mid-handler, postMessage on the un-focused one can race.
      await target_client.focus()
      target_client.postMessage({ type: 'notification-click', target })
      return
    }
    if (clients.openWindow) return clients.openWindow(target)
  })())
})