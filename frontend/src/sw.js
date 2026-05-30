/* Service worker — built into `/sw.js` by vite-plugin-pwa
 * (`injectManifest` strategy).
 *
 * What this file owns:
 *   - Precaching the Vite-hashed shell bundle. The manifest is
 *     INJECTED at build time via `self.__WB_MANIFEST`, so cache
 *     names are content-hashed automatically — no hand-edited
 *     `VERSION = 'vN'` to remember to bump.
 *   - Runtime caching for the few URLs that aren't part of the
 *     shell bundle but still benefit from caching: `/vendor/*`
 *     (immutable bundled libs), `esm.sh/*` (versioned remote
 *     deps), and `/api/proxy?url=*.{img|font|...}` (cacheable
 *     static assets via the CORS-bypass proxy).
 *   - Web Push handlers (push, notificationclick). These are
 *     domain-specific behavior that doesn't fit a Workbox recipe.
 *
 * What this file deliberately does NOT cache:
 *   - `/api/apps/{id}/{frame,module}` — the server returns an
 *     ETag derived from `app.updated_at`, and the browser HTTP
 *     cache handles revalidation natively. SW interception used
 *     to cache-first these and held stale modules across reloads
 *     (the "spinner-forever" bug class). See AGENTS.md "Service
 *     Worker" for the broader rationale.
 *   - HTML and other `/api/*` — straight to network.
 */

import {
  precacheAndRoute, cleanupOutdatedCaches, matchPrecache,
} from 'workbox-precaching'
import { registerRoute, setCatchHandler } from 'workbox-routing'
import {
  CacheFirst, StaleWhileRevalidate, NetworkFirst,
} from 'workbox-strategies'
import { clientsClaim } from 'workbox-core'
import {
  VENDOR_CACHE,
  ESM_CACHE,
  isCacheableAssetResponse,
  isStaleRuntimeCache,
} from './sw-cache-policy.js'

// LOAD-BEARING: these two calls are NOT injected by vite-plugin-pwa
// when using the `injectManifest` strategy + `injectRegister: null`
// (see vite.config.js). They are the only thing that makes the new
// SW take over without a user-initiated reload. Removing them
// breaks auto-update — installed PWAs would keep running the
// previous SW until every tab was closed.
//
// Interaction with the SSE `shell_rebuilt` event (Shell.jsx): when
// the agent rebuilds the shell, the backend emits `shell_rebuilt`
// and Shell.jsx does `window.location.reload()`. That reload is the
// authoritative refresh path. clientsClaim's silent SW swap is a
// fallback for the offline / SSE-missed case — the brief window
// where the new SW takes over an open tab running old JS is
// acceptable for a single-owner app that's almost always online
// when in use.
self.skipWaiting()
clientsClaim()

// Inject point — Workbox replaces this with the precache manifest
// derived from the Vite build's content-hashed assets. The result
// is that every release's shell precache lives under a unique
// content-versioned cache name; `cleanupOutdatedCaches()` purges
// older precaches when this SW activates.
precacheAndRoute(self.__WB_MANIFEST)
cleanupOutdatedCaches()

// On activate: evict stale runtime caches — the legacy hand-written
// `mobius-*-vN` caches AND the poisoned un-suffixed `mobius-vendor` /
// `mobius-esm` left on installs that hit the SPA-fallback bug. The
// current `-v2` caches are kept. See sw-cache-policy.js for the rules.
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys()
    await Promise.all(
      keys.filter(isStaleRuntimeCache).map(k => caches.delete(k)),
    )
  })())
})

// Refuse to cache an SPA-fallback HTML body (or an esm.sh `text/plain`
// error page) in the cache-first asset caches — the structural fix for
// the poisoning class. Returning null from `cacheWillUpdate` skips the
// cache write but still hands the response to the page (Workbox
// CacheFirst contract).
const assetCacheGuard = {
  cacheWillUpdate: async ({ response }) =>
    isCacheableAssetResponse(response) ? response : null,
}

// /vendor/* — immutable bundled libs (three.js etc.). Vite copies
// these in unchanged; URLs aren't content-hashed but the bytes are
// stable per release. Cache-first matches the prior behavior.
registerRoute(
  ({ url }) =>
    url.origin === self.location.origin && url.pathname.startsWith('/vendor/'),
  new CacheFirst({ cacheName: VENDOR_CACHE, plugins: [assetCacheGuard] }),
)

// esm.sh/* — third-party module CDN. Their URLs encode the version
// (e.g. `esm.sh/react@18.3.1`) so same URL = same content; safe to
// cache-first indefinitely.
registerRoute(
  ({ url }) => url.hostname === 'esm.sh',
  new CacheFirst({ cacheName: ESM_CACHE, plugins: [assetCacheGuard] }),
)

// /api/proxy — server-side CORS bypass. Only cache asset
// extensions (images, fonts, audio, video). JSON APIs and other
// dynamic responses bypass the cache by not matching this route
// so they go straight to network.
const CACHEABLE_PROXY_EXT =
  /\.(jpg|jpeg|png|gif|webp|svg|ico|woff2?|ttf|otf|eot|hdr|exr|mp3|mp4|webm|ogg|wav)(\?|$)/i

registerRoute(
  ({ url }) => {
    if (url.origin !== self.location.origin) return false
    if (url.pathname !== '/api/proxy') return false
    const upstream = url.searchParams.get('url') || ''
    return CACHEABLE_PROXY_EXT.test(upstream)
  },
  new StaleWhileRevalidate({ cacheName: 'mobius-proxy' }),
)

// ── Offline support ─────────────────────────────────────────────
//
// One root-scoped SW controls the shell (/shell/*), the bare domain
// (308 → /shell/), and standalone mini-apps (/apps/<slug>/*). The
// rule that keeps an installed PWA in standalone display mode offline:
// every navigation must resolve to a same-origin Response from the SW.
// If a navigation falls through to the network and fails (offline),
// the browser renders its NATIVE error page, which on Android exits
// standalone mode and reveals browser chrome. So all navigations are
// handled here, and setCatchHandler guarantees a fallback Response.
//
// These caches are DURABLE by design (they hold offline data) — they
// are NOT swept on activate; only logout clears them (client.js
// wipes all mobius-* caches).

// Cache a response only when the server marks the app offline-capable
// (X-Mobius-Offline header, set by routes/apps.py + standalone.py for
// offline_capable apps). Non-capable apps are never cached, so their
// current network-only behavior is preserved exactly. Cacheability is
// a function of server state, mirroring the ETag freshness model.
const offlineCapableOnly = {
  cacheWillUpdate: async ({ response }) =>
    response && response.headers.get('X-Mobius-Offline') === '1'
      ? response
      : null,
}

// The module URL carries a rotating auth token (and a retry `_=` buster);
// strip both so the cache key is stable across token rotation — otherwise
// every load is a cache miss and the offline entry is unreachable.
const stableModuleKey = {
  cacheKeyWillBeUsed: async ({ request }) => {
    const u = new URL(request.url)
    u.searchParams.delete('token')
    u.searchParams.delete('_')
    return u.href
  },
}

// Shell data GETs — last-known theme/app-list/chat-list so a cold
// offline launch renders chrome + drawer instead of throwing. SWR:
// serve cache, revalidate when online. Owner-scoped; wiped on logout.
registerRoute(
  ({ url }) =>
    url.origin === self.location.origin &&
    (url.pathname === '/api/theme' ||
      url.pathname === '/api/apps/' ||
      url.pathname === '/api/chats'),
  new StaleWhileRevalidate({ cacheName: 'mobius-shell-data' }),
)

// App frame/module — cached only for offline-capable apps (header
// gate). NetworkFirst keeps the network authoritative online (fresh
// module + ETag revalidation), so this does NOT reintroduce the
// stale-module bug class (that was CacheFirst). Offline → cached.
registerRoute(
  ({ url }) => /^\/api\/apps\/\d+\/(frame|module)$/.test(url.pathname),
  new NetworkFirst({
    cacheName: 'mobius-offline-apps',
    networkTimeoutSeconds: 5,
    plugins: [offlineCapableOnly, stableModuleKey],
  }),
)

// Shell + bare-domain navigations: network wins online (fresh
// theme-injected HTML + current asset hashes), cache serves offline.
registerRoute(
  ({ request, url }) =>
    request.mode === 'navigate' && !url.pathname.startsWith('/apps/'),
  new NetworkFirst({
    cacheName: 'mobius-shell-nav',
    networkTimeoutSeconds: 4,
  }),
)

// Standalone mini-app navigations: cached only for offline-capable
// apps (header gate). A non-capable app offline caches nothing →
// NetworkFirst throws → catch handler serves the branded offline page.
registerRoute(
  ({ request, url }) =>
    request.mode === 'navigate' && url.pathname.startsWith('/apps/'),
  new NetworkFirst({
    cacheName: 'mobius-standalone',
    networkTimeoutSeconds: 4,
    plugins: [offlineCapableOnly],
  }),
)

// Last resort for any document we still couldn't serve: the cached
// shell for /shell/*, the branded offline page for standalone +
// everything else. matchPrecache resolves the content-hashed entry.
setCatchHandler(async ({ request, url }) => {
  if (request.destination !== 'document') return Response.error()
  if (!url.pathname.startsWith('/apps/')) {
    return (
      (await matchPrecache('/index.html')) ||
      (await matchPrecache('/offline.html')) ||
      Response.error()
    )
  }
  return (await matchPrecache('/offline.html')) || Response.error()
})

// ── Web Push ────────────────────────────────────────────────────
//
// Pure-domain behavior — Workbox has no stock recipe for these,
// so we own them verbatim. Keep complete; an earlier truncation
// of this file failed `node --check` and silently disabled the
// SW for installed PWAs (no push, no offline cache).

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

// Whitelist notification targets to same-origin chat/app paths so a
// malicious payload (server compromise, MITM of an unencrypted push)
// can't steer openWindow() or postMessage to an arbitrary URL.
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
    // window they're actually using. Fall back to the first match
    // if nothing is visible.
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
