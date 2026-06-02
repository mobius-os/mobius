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
 * Caching model for `/api/apps/{id}/{frame,module}` (offline-capable apps):
 *   - These ARE cached, by `offlineCapableHandler`, but ONLY for apps the
 *     server marks `X-Mobius-Offline: 1` (offline_capable). The strategy is
 *     connectivity-aware: NETWORK-FIRST when we're known-online (so an agent's
 *     app edit is fresh on the current open), CACHE-FIRST when we're not
 *     known-online (instant offline open + background revalidate). "Known
 *     online" is the PAGE's /api/health probe verdict, posted to this SW (see
 *     the connectivity channel below) — NOT navigator.onLine, which reads
 *     stale-true while offline on Android PWAs.
 *   - cache:'reload' on the network attempt avoids the 304-no-body trap that
 *     previously left the module uncached (the old "spinner-forever" class).
 *   - Non-offline-capable apps are NEVER cached (no X-Mobius-Offline header) →
 *     always network, never stale.
 *   - HTML/shell navigations: StaleWhileRevalidate (instant cached shell);
 *     other `/api/*`: straight to network.
 */

import {
  precacheAndRoute, cleanupOutdatedCaches, matchPrecache,
  createHandlerBoundToURL,
} from 'workbox-precaching'
import {
  registerRoute, setCatchHandler, NavigationRoute,
} from 'workbox-routing'
import {
  CacheFirst, StaleWhileRevalidate, NetworkFirst,
} from 'workbox-strategies'
import { clientsClaim } from 'workbox-core'
import {
  VENDOR_CACHE,
  ESM_CACHE,
  isCacheableAssetResponse,
  isStaleRuntimeCache,
  isKnownOnline,
  shouldServeCacheFirst,
  shouldFallBackToCacheOnError,
  VERDICT_MAX_AGE_MS,
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
const SW_VERSION = '2026-06-02-shell-nav-precache'

// ── Page → SW connectivity channel ───────────────────────────────────
// The AUTHORITATIVE connectivity signal lives in the page: useOnlineStatus
// probes /api/health (a real network round-trip), which is trustworthy where
// `navigator.onLine` lies — notably an Android installed PWA reads
// navigator.onLine === true while genuinely offline. The SW cannot run that
// probe meaningfully itself, and reading navigator.onLine in the SW inherits
// the same lie. So the page POSTS its probe verdict here and the SW caches it,
// letting offlineCapableHandler take the instant cache-first path on a truly-
// offline open instead of waiting out a network timeout. While the verdict is
// `undefined` (cold SW restart before the first postMessage, or a stale verdict)
// we are NOT known-online, so a CACHED app is still served cache-first (instant
// — this is what removes the cold-restart race); only a cache MISS falls through
// to network-first. A wrong "offline" guess self-heals via the background
// revalidate, so erring cache-first on unknown connectivity is the right default
// for speed.
let _pageOnline   // true | false | undefined(unknown)
let _pageOnlineAt = 0
self.addEventListener('message', (e) => {
  const m = e.data
  if (!m || m.type !== 'moebius:connectivity') return
  if (typeof m.online === 'boolean') {
    _pageOnline = m.online
    _pageOnlineAt = Date.now()
  }
})
// Are we KNOWN to be online — i.e. is there a FRESH, POSITIVE page verdict? Used
// to gate the offline-capable frame/module fast path: a cached app is served
// cache-first UNLESS we're known-online (then network-first keeps the agent's
// edit→see-it-immediately loop fresh on the current open). "Not known online"
// covers genuinely-offline AND unknown (e.g. a cold SW restart before the first
// /api/health verdict postMessage arrives, or a stale verdict) — in all of those
// we prefer the instant cached app, which is safe because a wrong "offline"
// guess costs at most ONE stale open: the detached revalidate() refreshes the
// cache, and an app edit additionally remounts the iframe (version bump) on
// reconnect. Requiring a POSITIVE online proof (not merely the absence of an
// offline proof) is what makes the cold-restart first open instant WITHOUT a
// race against the verdict postMessage. See web.dev two-way-communication +
// Workbox #2892 for the pattern.
function knownOnline() {
  return isKnownOnline(_pageOnline, _pageOnlineAt, Date.now(), VERDICT_MAX_AGE_MS)
}

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
// Offline-capable frame/module strategy: CACHE-FIRST unless KNOWN-ONLINE (see
// the handler). A cached app is served instantly + revalidated in the background
// unless there's a fresh POSITIVE online verdict from the page (knownOnline()),
// in which case NETWORK-FIRST keeps the agent's edit→see-it loop fresh on the
// current open. The connectivity signal is the page's /api/health probe verdict
// (posted to this SW), NOT navigator.onLine (stale-true offline on Android).
//
// NET_TIMEOUT_MS bounds the network attempt — it is a HANG-GUARD for the
// pathological Android "fetch stays pending forever" case, NOT a latency lever.
// Kept at 3000: a real frame/module fetch on a slow-but-online 2G/3G connection
// can legitimately exceed a shorter bound, and aborting it would SILENTLY serve
// stale cached app code to an online user (an adversarial review flagged 1.5s as
// exactly this regression). The genuinely-offline fast path comes from the
// knownOnline() gate (instant cache-first when not known-online), not from
// shrinking this guard into the range of real slow fetches.
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
  return async ({ request, event }) => {
    const cache = await caches.open(cacheName)
    // The module URL carries a rotating auth token and a retry `_=` buster;
    // strip both so the cache key is stable across token rotation (else
    // every load is a miss and the offline entry is unreachable).
    const key = new URL(request.url)
    key.searchParams.delete('token')
    key.searchParams.delete('_')
    const cacheKey = key.href

    // The (bounded) network fetch that refreshes the cache. cache:'reload'
    // bypasses the browser HTTP cache so we get a full 200 body, never a 304
    // (see the 304-trap note above). Constructing a Request from a navigate-
    // mode request with an init throws in some engines; the builder falls back
    // to a plain same-origin GET.
    const revalidate = async () => {
      const resp = await boundedFetch((signal) => {
        try {
          return new Request(request, { cache: 'reload', signal })
        } catch {
          return new Request(request.url, { cache: 'reload', credentials: 'same-origin', signal })
        }
      })
      if (resp && resp.status === 200) {
        if (resp.headers.get('X-Mobius-Offline') === '1') {
          await cache.put(cacheKey, resp.clone())
        } else {
          // 200 but no offline header → the app was toggled offline_capable OFF.
          // Purge the now-stale entry so a not-known-online open stops serving
          // it cache-first (otherwise it would persist forever — cache.put is
          // skipped for header-less responses, so the old body never gets
          // overwritten). Self-heals a de-capabled app on the next refresh.
          await cache.delete(cacheKey)
        }
      }
      return resp
    }

    const cached = await cache.match(cacheKey)

    // CACHE-FIRST unless KNOWN-ONLINE. A cached offline-capable app is served
    // instantly + revalidated in the background UNLESS we have a fresh positive
    // online verdict from the page. Requiring positive online proof (not the
    // mere absence of an offline proof) is the key: on a cold SW restart the
    // verdict postMessage hasn't arrived yet (_pageOnline=undefined), so
    // knownOnline() is false and we serve the cached app INSTANTLY on the very
    // first request — no race, no timeout. We do NOT key off navigator.onLine
    // (it reads stale-true offline on the target Android device).
    //
    // Safety of guessing-offline-while-actually-online (cold-restart online, or
    // a stale verdict): the detached revalidate() refreshes the cache in the
    // background, and an app edit remounts the iframe (version bump) on
    // reconnect — so a wrong guess costs at most ONE stale open, self-healing.
    // When genuinely online with the SW warm, knownOnline() is true → network-
    // first → the agent's edit is fresh on the current open (the build loop the
    // first review insisted on). NET_TIMEOUT_MS stays a generous hang-guard on
    // the network path, never a short latency lever (a slow-but-real fetch must
    // not be aborted into a stale serve).
    if (shouldServeCacheFirst(!!cached, knownOnline())) {
      // Background-refresh, but tie it to the fetch event's lifetime so the
      // browser keeps the SW alive until the fetch + cache.put finish — a bare
      // detached promise can be cut off when the worker is terminated right
      // after the cached response returns, leaving stale code cached for the
      // next open (Codex review). event may be absent in non-FetchEvent
      // dispatch; fall back to detached then.
      const refresh = revalidate().catch(() => {})  // rejects harmlessly when offline
      if (event && typeof event.waitUntil === 'function') event.waitUntil(refresh)
      return cached
    }

    // Known-online (or no cached copy yet): network-first. On success the
    // freshest body is served on THIS open + the cache is refreshed for the next
    // offline open. A transient SERVER error (5xx) must not blank a cached app —
    // fall back to the cached copy for those (but NOT 4xx, which are
    // authoritative: a 404 means the app is gone, a 401/403 is real auth). On a
    // network REJECTION (offline / abort / DNS) the catch serves cache too.
    try {
      const resp = await revalidate()
      if (resp && shouldFallBackToCacheOnError(resp.status, !!cached)) return cached
      return resp
    } catch (err) {
      const late = await cache.match(cacheKey)
      if (late) return late
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
    // KEPT at 5s deliberately. Workbox returns a cache fallback as a
    // successful fetch that TanStack can't distinguish from a confirmed live
    // response (isFetchedAfterMount), so a too-fast fallback of a stale `[]`
    // could wrongly trip Shell.jsx's auto-create-starter-chat. /api/chats does
    // NOT gate the visible shell render — the shell-nav route does (now 2s) —
    // so there's no load-speed reason to shorten this; the correctness risk
    // wins. (Codex review Medium #2.)
    networkTimeoutSeconds: 5,
  }),
)

// App frame/module — offline-capable apps only (X-Mobius-Offline:1). Served by
// offlineCapableHandler: cache-first-unless-known-online (instant offline,
// fresh-on-current-open when known-online) — see its full contract above. The
// network attempt uses cache:'reload' to dodge the NetworkFirst+304 trap that
// once left the module uncached and blanked the in-shell iframe offline.
registerRoute(
  ({ url }) => /^\/api\/apps\/\d+\/(frame|module)$/.test(url.pathname),
  offlineCapableHandler('mobius-offline-apps'),
)

// Shell + bare-domain navigations: serve the PRECACHED index.html — the
// canonical Workbox app-shell pattern. Instant offline (a precache read, no
// network race, no timeout — this is what removes the multi-second cold-open
// wait), and ALWAYS consistent with the precached bundle.
//
// Why precache, NOT a separate StaleWhileRevalidate cache (the bug this fixes):
// a SWR `mobius-shell-nav` cache stored the full index.html, INCLUDING its
// content-hashed `<script src="/assets/index-<hash>.js">`, and that cache was
// never purged on an SW update. After a deploy bumped the bundle hash, the new
// SW's cleanupOutdatedCaches() deleted the OLD precache (old bundle gone), but
// the stale index.html survived in mobius-shell-nav. Offline — where the
// updatefound watchdog can't fire (it needs a network sw.js fetch) and the
// background revalidate can't reach the network — the navigation served that
// stale HTML, whose `<script>` pointed at a hash that no longer existed in the
// precache OR on the server → the bundle never loaded → index.html's 8s
// watchdog showed "Shell failed to load". createHandlerBoundToURL resolves the
// precached index.html, which Workbox keeps in lockstep with the precached
// bundle (shared revision manifest + cleanupOutdatedCaches), so HTML and bundle
// can never disagree. Theme still applies client-side from
// localStorage('mobius-theme-bg') before first paint, so the cached
// (non-theme-injected) HTML renders correctly with no server round-trip.
registerRoute(new NavigationRoute(
  createHandlerBoundToURL('/index.html'),
  { denylist: [/^\/apps\//] },
))

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
