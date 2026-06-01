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

// Identifies THIS service-worker generation. Bump on any meaningful SW
// change so /diag.html can confirm which SW a device is actually running
// (the whole class of "did my fix even reach the phone?" questions — a SW
// only updates on an online visit, so an installed PWA can run an old SW
// for a while). Served offline by the route below because the SW
// synthesizes the response; no network needed.
const SW_VERSION = '2026-06-01-offline-hang-fix'

// /api/__sw_version — a synthetic, SW-generated response so /diag.html can
// read the live SW generation even offline. Registered before the catch
// handler; never hits the network.
registerRoute(
  ({ url }) => url.pathname === '/api/__sw_version',
  async () =>
    new Response(
      JSON.stringify({ version: SW_VERSION, ts: Date.now() }),
      { headers: { 'Content-Type': 'application/json' } },
    ),
)

// Self-hosted React for the mini-app import map. These live in
// /app/static/vendor (copied by the Dockerfile AFTER the Vite build, so
// Vite's manifest can't glob them) and are referenced by the import maps
// in app-frame.html (in-shell iframe) and standalone.py (installed PWA).
// They MUST be PRECACHED, not left to the runtime CacheFirst /vendor route
// below: that route only fills lazily after a successful ONLINE fetch of
// each exact URL, so on an installed PWA the React URLs were never warmed
// and the iframe's STATIC `import 'react-dom/client'` failed offline —
// aborting the whole module before any error UI, a silent blank screen.
// Precaching makes React install-time guaranteed offline, the same tier as
// the shell bundle. The version in the path is the cache-bust (revision:
// null); bumping React here means bumping it in app-frame.html + the
// standalone.py import map + the Dockerfile vendor step in lockstep.
// Registered in the SAME precacheAndRoute as the shell so its precache
// route takes precedence over the runtime /vendor CacheFirst route below
// (first-registered route wins) — the precached copy shadows any stale
// runtime-cached vendor entry from before this fix.
const REACT_VENDOR = '/vendor/react@19.2.6'
const VENDORED_REACT = [
  `${REACT_VENDOR}/core.mjs`,
  `${REACT_VENDOR}/react.mjs`,
  `${REACT_VENDOR}/react-dom.mjs`,
  `${REACT_VENDOR}/client.mjs`,
  `${REACT_VENDOR}/jsx-runtime.mjs`,
].map(url => ({ url, revision: null }))

// Inject point — Workbox replaces `self.__WB_MANIFEST` with the precache
// manifest derived from the Vite build's content-hashed assets. The result
// is that every release's shell precache lives under a unique
// content-versioned cache name; `cleanupOutdatedCaches()` purges
// older precaches when this SW activates.
precacheAndRoute([...self.__WB_MANIFEST, ...VENDORED_REACT])
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

// Offline runtime caching for the per-app frame + module and the
// standalone-app navigation. Only stores responses the server marks
// offline-capable (X-Mobius-Offline header, set by routes/apps.py +
// standalone.py for offline_capable apps), so non-capable apps keep
// their network-only behavior exactly.
//
// Why a hand-written handler instead of NetworkFirst + a cacheWillUpdate
// gate: these routes carry an ETag and `Cache-Control: no-cache`. Once the
// browser HTTP-caches a response, the SW's own `fetch(request)` revalidates
// with If-None-Match and the server answers `304 Not Modified` — which has
// no body and status 304, so the offline-capable gate (status === 200)
// rejected it and NOTHING was ever written to the offline cache. The app's
// module was then absent offline and the in-shell iframe's dynamic
// `import()` of /module rejected, blanking the app (React kept working only
// because it is precached). The 304 made this intermittent and is why it
// survived several server-side fixes — the broken artifact lived in the
// device cache, untouched by any server change.
//
// The fix: fetch with `cache: 'reload'` to BYPASS the browser HTTP cache,
// so every online load is a full 200 with a body (never a 304); store that
// under a token-stripped key; serve the stored copy when the network fails.
// This makes offline availability a deterministic function of "was it
// loaded online once," independent of HTTP-cache revalidation state.
// Bounded NETWORK-FIRST. The route stays network-first to preserve online
// freshness — these responses carry a server ETag and an agent's app edit must
// be seen on the next online load, so a cache-first serve (which would boot
// stale app code after an edit) is NOT acceptable. The ONLY change from a bare
// network-first is a timeout: offline on Android the browser's connectivity
// state can be stale, so a same-origin `fetch()` stays PENDING (never resolves,
// never rejects) instead of failing fast — the "navigator.onLine lies"
// symptom. The old handler did `await fetch(...)` with no timeout, so when the
// network hung the cache-fallback `catch` NEVER ran: the iframe's
// `import('/module')` waited forever and the app span. (Confirmed on-device:
// the diag log ended on `module:import:start` with no `module:import:ok`.) Now
// the network attempt is bounded and AbortController-cancelled on timeout, then
// we fall back to the cached copy — fixing the hang without sacrificing online
// freshness.
const NET_TIMEOUT_MS = 3000

// Run ONE fetch, bounded by NET_TIMEOUT_MS, aborting the underlying request on
// timeout so pending offline fetches can't accumulate across repeated opens.
// `buildRequest(signal)` constructs the Request (its own throw is caught by the
// handler → cache fallback, so we never spin a second parallel fetch). Rejects
// on timeout, abort, or network failure — the handler treats all the same.
async function boundedFetch(buildRequest) {
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
  const req = buildRequest(ctrl ? ctrl.signal : undefined)
  let timer
  try {
    const p = fetch(req)
    if (!ctrl) {
      // Engine without AbortController: still bound the wait (underlying fetch
      // may linger — best-effort).
      return await Promise.race([
        p,
        new Promise((_, rej) => { timer = setTimeout(() => rej(new Error('sw-fetch-timeout')), NET_TIMEOUT_MS) }),
      ])
    }
    timer = setTimeout(() => ctrl.abort(), NET_TIMEOUT_MS)
    return await p
  } finally {
    if (timer) clearTimeout(timer)
  }
}

function offlineCapableHandler(cacheName) {
  return async ({ request }) => {
    const cache = await caches.open(cacheName)
    // The module URL carries a rotating auth token and a retry `_=` buster;
    // strip both so the cache key is stable across token rotation (else
    // every load is a miss and the offline entry is unreachable).
    const key = new URL(request.url)
    key.searchParams.delete('token')
    key.searchParams.delete('_')
    const cacheKey = key.href
    try {
      // cache:'reload' bypasses the browser HTTP cache so we get a full 200
      // body, never a 304 (see the 304-trap note above). Constructing a
      // Request from a navigate-mode request with an init throws in some
      // engines; the builder falls back to a plain same-origin GET. Any throw
      // from the builder or the fetch is caught below → cache fallback.
      const resp = await boundedFetch((signal) => {
        try {
          return new Request(request, { cache: 'reload', signal })
        } catch {
          return new Request(request.url, { cache: 'reload', credentials: 'same-origin', signal })
        }
      })
      if (
        resp && resp.status === 200 &&
        resp.headers.get('X-Mobius-Offline') === '1'
      ) {
        await cache.put(cacheKey, resp.clone())
      }
      return resp
    } catch (err) {
      // Timed out, aborted, or network failed → serve the cached copy if we
      // have one. This is the path that fixes the offline hang.
      const cached = await cache.match(cacheKey)
      if (cached) return cached
      throw err
    }
  }
}

// Shell data GETs — last-known theme + app-list so a cold offline
// launch renders chrome + drawer instead of throwing. SWR: serve
// cache, revalidate when online. Owner-scoped; wiped on logout.
registerRoute(
  ({ url }) =>
    url.origin === self.location.origin &&
    (url.pathname === '/api/theme' ||
      url.pathname === '/api/apps/'),
  new StaleWhileRevalidate({ cacheName: 'mobius-shell-data' }),
)

// `/api/chats` — same cache bucket as above (one logical
// shell-data store) but NetworkFirst instead of SWR. The two
// strategies share `cacheName` cleanly because Workbox isolates
// cache storage from fetch strategy.
//
// Why split: Shell.jsx's auto-create-starter-chat effect needs an
// authoritative answer for "are there chats?" before it POSTs a
// new one. SWR returns the cached body the same tick the fetch
// fires; under a fast-online network that effectively erases the
// "did a fetch resolve after Shell mounted?" signal the auto-
// create effect relies on. NetworkFirst keeps the hot path going
// to the network so the live response, not the cache, is what the
// effect sees. The 5s timeout still permits a cached-`[]` fallback
// on a degraded (but technically online) connection — that's the
// narrow residual window where the auto-create can over-fire, but
// the duplicate is recoverable and far less likely than under SWR.
//
// Offline / >5s → cache wins, drawer keeps showing the last-known
// list. This is the offline-feature agent's stated contract; the
// SW cache outlives TanStack Query's 24h persister maxAge so it's
// the durable layer for cold-offline launches. See Shell.jsx's
// isFetchedAfterMount comment for the consumer-side of this fix.
registerRoute(
  ({ url }) =>
    url.origin === self.location.origin &&
    url.pathname === '/api/chats',
  new NetworkFirst({
    cacheName: 'mobius-shell-data',
    networkTimeoutSeconds: 5,
  }),
)

// App frame/module — stored for offline-capable apps via the
// reload-bypass handler (see offlineCapableHandler). Network wins online
// (always a fresh 200 body, so no stale module), cache serves offline.
// This is the route whose NetworkFirst+304 interaction left the module
// uncached and blanked the in-shell iframe offline.
registerRoute(
  ({ url }) => /^\/api\/apps\/\d+\/(frame|module)$/.test(url.pathname),
  offlineCapableHandler('mobius-offline-apps'),
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

// Standalone mini-app navigations: stored for offline-capable apps via
// the same reload-bypass handler — the standalone page carries the same
// ETag + `Cache-Control: no-cache`, so it had the identical 304-never-
// cached defect as the frame/module route. A non-capable app caches
// nothing → handler rethrows offline → catch handler serves the branded
// offline page.
registerRoute(
  ({ request, url }) =>
    request.mode === 'navigate' && url.pathname.startsWith('/apps/'),
  offlineCapableHandler('mobius-standalone'),
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
