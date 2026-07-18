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
 * Caching model for `/api/apps/{id}/{frame,module}` (ALL installed apps):
 *   - These ARE cached, by `appCodeHandler`, for EVERY installed app — loading
 *     speed must not depend on the manifest's offline_capable flag. Once
 *     cached, the strategy is cache-first in every connectivity state: the
 *     cached app opens immediately and a background fetch refreshes the cache.
 *     Freshness comes from the versioned frame/module URLs
 *     (`?v=<app.updated_at>` is part of the cache key), so an app update
 *     naturally becomes a cache miss and loads from the network on its first
 *     open.
 *   - cache:'reload' on the network attempt avoids the 304-no-body trap that
 *     previously left the module uncached (the old "spinner-forever" class).
 *   - The offline_capable flag (`X-Mobius-Offline: 1` response header) still
 *     gates the STANDALONE navigation cache + offline write semantics — a
 *     non-capable app's /apps/<slug>/ page is never stored, so its offline
 *     open keeps showing the branded offline page exactly as before. Only the
 *     in-shell read path (frame/module) is flag-independent.
 *   - HTML/shell navigations: served from the Workbox precache via
 *     `NavigationRoute(createHandlerBoundToURL('/index.html'))` — NOT
 *     StaleWhileRevalidate; the shell deliberately avoids SWR.
 *   - Several `/api/*` routes are cached rather than going straight to
 *     network: `/api/theme` is StaleWhileRevalidate, `/api/chats` and
 *     `/api/apps/` are NetworkFirst (cache fallback when offline), and
 *     `/api/apps/{id}/{frame,module}` go through the cache-first
 *     `appCodeHandler` above.
 */

import {
  precacheAndRoute, cleanupOutdatedCaches, matchPrecache,
  createHandlerBoundToURL, addPlugins,
} from 'workbox-precaching'
import {
  registerRoute, setCatchHandler, NavigationRoute,
} from 'workbox-routing'
import {
  CacheFirst, StaleWhileRevalidate, NetworkFirst,
} from 'workbox-strategies'
import {
  VENDOR_CACHE,
  ESM_CACHE,
  OFFLINE_APPS_CACHE,
  STANDALONE_APPS_CACHE,
  APP_ASSETS_CACHE,
  APP_ASSETS_MAX_ENTRIES,
  isCacheableAssetResponse,
  isOpaqueFramePublicAssetPath,
  withOpaqueFramePublicAssetCors,
  isCacheableAppAssetResponse,
  isImmutableAppAsset,
  isPackagedAppAsset,
  packagedAppAssetCacheKey,
  hasOpaqueEmbedSandbox,
  isCacheableOpaqueEmbedDocument,
  isRangeRequest,
  isStaleRuntimeCache,
  shouldServeCacheFirst,
  shouldFallBackToCacheOnError,
  isAppCodeRoute,
  appCodeStoreAction,
  supersededVersionKeys,
  entriesToTrim,
} from './sw-cache-policy.js'

const isOpaqueFramePublicAssetRequest = request => {
  try {
    return isOpaqueFramePublicAssetPath(new URL(request.url).pathname)
  } catch {
    return false
  }
}

// Repair opaque-frame public assets at the service-worker boundary, including
// entries written by an older release before the server made them intrinsically
// CORS-readable. This hook runs on precache hits as well as fresh installs.
const opaqueFramePublicAssetCorsPlugin = {
  cachedResponseWillBeUsed: async ({ request, cachedResponse }) =>
    isOpaqueFramePublicAssetRequest(request)
      ? withOpaqueFramePublicAssetCors(cachedResponse)
      : cachedResponse,
  fetchDidSucceed: async ({ request, response }) =>
    isOpaqueFramePublicAssetRequest(request)
      ? withOpaqueFramePublicAssetCors(response)
      : response,
}

// SW UPDATE LEASH (design §1.3 — "the streaming view is sacred").
//
// We deliberately do NOT skipWaiting()/clientsClaim() at the top level. Under
// the `injectManifest` strategy + `injectRegister: null` (see vite.config.js)
// those are the only auto-takeover calls, and an eager takeover is exactly the
// disruption this design removes: a new SW that activates + claims while a tab
// is mid-turn flips the generation underneath a LIVE stream — its lazy chunks
// 404, and `controllerchange` can force-reload the page mid-turn. So the new SW
// now INSTALLS AND WAITS. The page hands it control — postMessage
// {type:'SKIP_WAITING'} from Shell's performShellReload — only at an idle apply
// boundary (Shell holds the reload while the owner is typing/steering/reading a
// running chat), so the SW generation flips exactly when the page generation
// does.
//
// FIRST-EVER INSTALL is the one case that still claims immediately: there is no
// old generation whose live turn could be disrupted, and claiming lets offline
// caching + app-code cache-first work from the very first load (an uncontrolled
// page's fetches bypass the SW entirely). We detect it at install time —
// `self.registration.active` is the OUTGOING worker during our install and is
// null on a first-ever install — and claim on activate only in that case. The
// module-level flag carries that verdict from install to activate (both fire on
// this same worker's global scope). On an UPDATE we never reach activate until
// the page posted SKIP_WAITING at its idle boundary; the reload Shell then
// drives adopts the freshly-active worker without any spontaneous claim.
let isFirstInstall = false
self.addEventListener('install', () => {
  isFirstInstall = !self.registration.active
})
self.addEventListener('message', (event) => {
  // The page reached its idle apply-boundary and asked us to take over.
  // This is the ONLY place the SW leaves the waiting phase on an update.
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting()
})

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
const REACT_VENDOR = '/vendor/react@19.2.7'
const VENDORED_REACT = [
  `${REACT_VENDOR}/core.mjs`,
  `${REACT_VENDOR}/react.mjs`,
  `${REACT_VENDOR}/react-dom.mjs`,
  `${REACT_VENDOR}/client.mjs`,
  `${REACT_VENDOR}/jsx-runtime.mjs`,
].map(url => ({ url, revision: null }))

// Self-hosted recharts + date-fns — same precache rationale as React above.
// Mini-apps that render charts / format dates import these from the importmap.
// Left to the runtime CacheFirst /vendor route they would only warm on the
// first ONLINE open of a chart/date app; an installed PWA that opens a chart
// app offline for the first time would import-fail. Precaching makes them
// install-time guaranteed offline. Version in the path is the cache-bust.
const VENDORED_RECHARTS = [
  { url: '/vendor/recharts@2.15.4/recharts.mjs', revision: null },
]
const VENDORED_DATE_FNS = [
  { url: '/vendor/date-fns@4.3.0/date-fns.mjs', revision: null },
]

// Self-hosted d3-geo (Atlas globe) + marked & DOMPurify (Notes markdown
// preview) — same precache rationale as React above. These were the last three
// libs Atlas/Notes still pulled from esm.sh; left to the runtime CacheFirst
// /vendor route they'd only warm on the first ONLINE open, so an installed PWA
// opening Atlas/Notes offline the first time would import-fail (the globe or the
// note-card previews). Precaching makes them install-time offline-guaranteed.
// Bumping a version here means bumping it in app-frame.html's import map + the
// Dockerfile vendor step in lockstep.
const VENDORED_ATLAS_NOTES = [
  { url: '/vendor/d3-geo@3.1.1/d3-geo.mjs', revision: null },
  { url: '/vendor/marked@17.0.6/marked.mjs', revision: null },
  { url: '/vendor/dompurify@3.4.11/dompurify.mjs', revision: null },
]

// Self-hosted CodeMirror 6 — same precache rationale as React above. The
// Notes / LaTeX / Editor / Web Studio apps STATICALLY import @codemirror/* +
// @lezer/highlight + the `codemirror` meta-package at module top via the
// import maps; left to the runtime CacheFirst /vendor route they were never
// warmed on an installed PWA, so offline the import rejected and took the
// whole app (incl. the LaTeX PDF viewer) down. Precaching makes them
// install-time offline-guaranteed. Bumping the version here means bumping it
// in app-frame.html's import map + the Dockerfile vendor step in lockstep
// (standalone.py derives its import map from app-frame.html via importmap_block).
const CODEMIRROR_VENDOR = '/vendor/codemirror@6'
const VENDORED_CODEMIRROR = [
  `${CODEMIRROR_VENDOR}/core.mjs`,
  `${CODEMIRROR_VENDOR}/codemirror.mjs`,
  `${CODEMIRROR_VENDOR}/state.mjs`,
  `${CODEMIRROR_VENDOR}/view.mjs`,
  `${CODEMIRROR_VENDOR}/commands.mjs`,
  `${CODEMIRROR_VENDOR}/language.mjs`,
  `${CODEMIRROR_VENDOR}/lang-markdown.mjs`,
  `${CODEMIRROR_VENDOR}/lezer-highlight.mjs`,
].map(url => ({ url, revision: null }))

// Self-hosted d3 + PixiJS for the Memory graph — same precache rationale as
// React above. Unlike the importmap libs these are CLASSIC script-tag loads
// (the app's loadScriptOnce expects window.d3 / window.PIXI globals); they
// were previously fetched from cdn.jsdelivr.net, which the prod CSP
// (script-src 'self' ... https://esm.sh) blocks — the graph silently
// degraded to the list view and could never work offline. The files are
// committed at frontend/public/vendor/ (Vite copies public/ verbatim, so no
// Dockerfile npm-install step); the version in the path is the cache-bust.
// Bumping a version here means bumping the dir under frontend/public/vendor/
// + the D3_URL / PIXI_URL constants in core-apps/memory/index.jsx in lockstep.
const VENDORED_MEMORY_GRAPH = [
  '/vendor/d3@7.9.0/d3.min.js',
  '/vendor/pixi.js@8.19.0/pixi.min.js',
].map(url => ({ url, revision: null }))

// Inject point — Workbox replaces `self.__WB_MANIFEST` with the precache
// manifest derived from the Vite build's content-hashed assets. The result
// is that every release's shell precache lives under a unique
// content-versioned cache name; `cleanupOutdatedCaches()` purges
// older precaches when this SW activates.
addPlugins([opaqueFramePublicAssetCorsPlugin])
precacheAndRoute([
  ...self.__WB_MANIFEST,
  ...VENDORED_REACT,
  ...VENDORED_CODEMIRROR,
  ...VENDORED_RECHARTS,
  ...VENDORED_DATE_FNS,
  ...VENDORED_ATLAS_NOTES,
  ...VENDORED_MEMORY_GRAPH,
])
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
    // First-ever install only: claim the (uncontrolled) page now so offline +
    // app-code cache-first work from the very first load. An UPDATE reaches
    // activate only after a page-initiated SKIP_WAITING; the reload the page
    // then drives adopts the new worker, so we never claim an already-controlled
    // page out from under a live turn. See the SW UPDATE LEASH note above.
    if (isFirstInstall) await self.clients.claim()
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
  new CacheFirst({
    cacheName: VENDOR_CACHE,
    plugins: [assetCacheGuard, opaqueFramePublicAssetCorsPlugin],
  }),
)

// esm.sh/* — third-party module CDN. Their URLs encode the version
// (e.g. `esm.sh/react@18.3.1`) so same URL = same content; safe to
// cache-first indefinitely.
registerRoute(
  ({ url }) => url.hostname === 'esm.sh',
  new CacheFirst({ cacheName: ESM_CACHE, plugins: [assetCacheGuard] }),
)

// /app-assets/{slug}/* — durable static files owned by packaged apps
// (CubeRun's ~19MB of models/textures re-downloaded on EVERY open before
// this). The server splits cache semantics by filename
// (_serve_app_static_asset in backend/app/main.py): content-hashed names
// are immutable — a re-install that changes the bytes changes the name —
// and everything else is no-cache + ETag/304. Mirror that split:
//   - hashed names: CacheFirst, the same tier as /vendor/ above. Once
//     stored we never refetch; entries superseded by a re-install are
//     left harmlessly behind, like stale vendor entries.
//   - the rest (index.html, un-hashed media): StaleWhileRevalidate —
//     instant cached serve + background refresh, which the server now
//     answers with a bodiless 304 when nothing changed. The foreground
//     path only blocks on a cache miss, where there is nothing to serve
//     yet (the NET_TIMEOUT_MS rationale below).
// A raw 304 reaching either strategy (request carried the browser's
// If-None-Match) is refused by cacheWillUpdate — non-200 never replaces
// a stored copy, and the browser synthesizes the body from its own HTTP
// cache, so there is no frame/module-style never-cached trap here.
// Ranged requests (`Range:` header) bypass BOTH routes — straight to the
// network, never matched against or stored in the cache. A response to a
// ranged fetch can come back from Chromium's HTTP cache as a status-200
// body holding only the slice (no 206/Content-Range marker), and caching
// that under the bare URL truncates the asset for every later consumer —
// CubeRun's `Range: bytes=0-0` probe blacked out the game this way
// (2026-06-12); the request side is the only place the case is visible.
// Bounded-growth trim for APP_ASSETS_CACHE. Neither route below carries a
// Workbox ExpirationPlugin (it's not a dep on these routes), and a single app
// can be ~19MB of assets, so without a cap the cache grows unbounded across
// installs and one asset-heavy app could evict the whole origin's quota.
// cacheDidUpdate fires after each successful write; we then trim the OLDEST
// entries (cache.keys() is insertion order) down to APP_ASSETS_MAX_ENTRIES via
// the unit-tested entriesToTrim helper. Best-effort: a trim failure never
// affects the served response.
const appAssetsTrimPlugin = {
  cacheDidUpdate: async ({ cacheName }) => {
    try {
      const cache = await caches.open(cacheName)
      const keys = (await cache.keys()).map(r => r.url)
      await Promise.all(
        entriesToTrim(keys, APP_ASSETS_MAX_ENTRIES).map(k => cache.delete(k)),
      )
    } catch {
      // ignore — the cache is over its soft cap for one extra entry at worst.
    }
  },
}

const packagedAssetCacheKeyPlugin = {
  cacheKeyWillBeUsed: async ({ request }) => {
    const isDocument = request.mode === 'navigate'
      || request.destination === 'document'
    return packagedAppAssetCacheKey(request.url, {
      isDocument,
      // fetch()/XHR has an empty destination. Preserve its namespace rather
      // than letting a response-sandboxed entry alias onto /app-assets.
      isSubresource: !isDocument && !!request.destination,
    })
  },
}

const packagedAssetUpdateGuard = {
  cacheWillUpdate: async ({ request, response }) => {
    const url = new URL(request.url)
    const documentRequest = request.mode === 'navigate'
      || request.destination === 'document'
    if (url.pathname.startsWith('/app-embeds/')) {
      // Namespace-wide invariant: a fetch/XHR must not put an unsandboxed
      // response into the same cache identity a later navigation can read.
      if (!hasOpaqueEmbedSandbox(response)) return null
      if (documentRequest) {
        return isCacheableOpaqueEmbedDocument(response) ? response : null
      }
    }
    return response?.status === 200 ? response : null
  },
}

registerRoute(
  ({ url, request }) =>
    url.origin === self.location.origin &&
    isImmutableAppAsset(url.pathname) &&
    request.mode !== 'navigate' &&
    request.destination !== 'document' &&
    !isRangeRequest(request),
  new CacheFirst({
    cacheName: APP_ASSETS_CACHE,
    plugins: [
      {
        cacheWillUpdate: async ({ response }) =>
          isCacheableAppAssetResponse(response) ? response : null,
      },
      packagedAssetCacheKeyPlugin,
      appAssetsTrimPlugin,
    ],
  }),
)
registerRoute(
  ({ url, request }) =>
    url.origin === self.location.origin &&
    isPackagedAppAsset(url.pathname) &&
    (!isImmutableAppAsset(url.pathname)
      || request.mode === 'navigate'
      || request.destination === 'document') &&
    !isRangeRequest(request),
  new StaleWhileRevalidate({
    cacheName: APP_ASSETS_CACHE,
    plugins: [
      packagedAssetCacheKeyPlugin,
      packagedAssetUpdateGuard,
      appAssetsTrimPlugin,
    ],
  }),
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

// Runtime caching for the per-app frame + module and the standalone-app
// navigation, via one shared handler with two storage policies (see
// appCodeStoreAction in sw-cache-policy.js):
//   - frame/module (gated=false): EVERY installed app's code is cached, so
//     every app gets the instant cache-first open. The offline_capable flag
//     no longer gates the read path.
//   - standalone navigations (gated=true): only stores responses the server
//     marks offline-capable (X-Mobius-Offline header, set by routes/apps.py +
//     standalone.py for offline_capable apps), so non-capable apps keep their
//     network-only standalone behavior exactly.
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
// Frame/module strategy: CACHE-FIRST once cached. A cached app is served
// instantly and revalidated in the background in every connectivity
// state. This is safe and fresh enough because AppCanvas includes
// `?v=<app.updated_at>` in the frame URL and the frame forwards that same
// version into the module URL; an app edit changes the cache key and forces a
// network load for the new version. Stale cached versions are NOT left behind
// until the bucket is cleared — they are pruned eagerly on the next successful
// store: after each `cache.put` of a new `?v=` version, applyAppCodeStore()
// calls supersededVersionKeys() to find prior versions of the same route (same
// pathname, different `?v=`) and deletes them, so the frame/module cache holds
// at most the current versioned pair per app.
//
// NET_TIMEOUT_MS bounds the network attempt — it is a HANG-GUARD for the
// pathological Android "fetch stays pending forever" case, NOT a latency lever.
// Kept at 3000: the foreground path only uses it on a cache miss, where there
// is nothing useful to serve yet. Cache hits return immediately; the timed
// network attempt only refreshes the stored copy in the background.
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

// Apply an appCodeStoreAction to the cache, tolerating a failed write. A
// cache.put can REJECT (QuotaExceededError when the origin is over its storage
// budget); that must NOT propagate, because the caller has already decided to
// serve the network response and a store failure is non-fatal — the app still
// loads online; only the offline copy is missed this turn. We also evict the
// superseded `?v=` versions of the SAME route on a successful store, so the
// frame/module cache can't grow one stale pair per app edit. Returns nothing;
// its rejection is swallowed by callers who run it under event.waitUntil.
async function applyAppCodeStore(cache, cacheKey, resp, gated) {
  const action = appCodeStoreAction(
    resp.status, resp.headers.get('X-Mobius-Offline'), gated,
  )
  if (action === 'purge') {
    await cache.delete(cacheKey)
    return
  }
  if (action !== 'store') return
  try {
    await cache.put(cacheKey, resp.clone())
  } catch {
    // QuotaExceeded (or any storage failure): the response is already being
    // served; skip caching this turn rather than letting the open hard-fail.
    return
  }
  // Drop the prior versions of THIS exact route — same pathname, different
  // `?v=`. Each app edit otherwise leaves its old versioned entry behind
  // forever (its `?v=` is never requested again), growing the cache without
  // bound. Best-effort: a failure here never affects the served response.
  try {
    const keys = (await cache.keys()).map(r => r.url)
    await Promise.all(
      supersededVersionKeys(cacheKey, keys).map(k => cache.delete(k)),
    )
  } catch {
    // ignore — eviction is opportunistic.
  }
}

function appCodeHandler(cacheName, { gated }) {
  return async ({ request, event }) => {
    const cache = await caches.open(cacheName)
    // The module URL carries a rotating auth token and a retry `_=` buster;
    // strip both so the cache key is stable across token rotation (else
    // every load is a miss and the offline entry is unreachable).
    const key = new URL(request.url)
    key.searchParams.delete('token')
    key.searchParams.delete('_')
    key.searchParams.delete('install')
    const cacheKey = key.href

    // The (bounded) network fetch that refreshes the cache. cache:'reload'
    // bypasses the browser HTTP cache so we get a full 200 body, never a 304
    // (see the 304-trap note above). Constructing a Request from a navigate-
    // mode request with an init throws in some engines; the builder falls back
    // to a plain same-origin GET.
    //
    // DECOUPLED store-from-serve: revalidate resolves with the network
    // response UNCONDITIONALLY and returns the store promise separately, so the
    // served response never depends on the cache.put. A QuotaExceeded put
    // rejection would otherwise discard a perfectly good NETWORK response — the
    // cold path's catch then misses cache.match and rethrows, hard-failing the
    // app WHILE ONLINE. The store runs as its own promise the caller hands to
    // event.waitUntil with its own .catch, mirroring the background-refresh
    // branch's tolerance; storage failures never reach the served response.
    const revalidate = async () => {
      const resp = await boundedFetch((signal) => {
        try {
          return new Request(request, { cache: 'reload', signal })
        } catch {
          return new Request(request.url, { cache: 'reload', credentials: 'same-origin', signal })
        }
      })
      // Storage policy lives in sw-cache-policy.js so it's unit-tested:
      // ungated (frame/module) stores every 200; gated (standalone) stores
      // only X-Mobius-Offline:1 and PURGES a header-less 200 so an app
      // toggled offline_capable OFF self-heals on the next refresh.
      const store = resp
        ? applyAppCodeStore(cache, cacheKey, resp, gated)
        : Promise.resolve()
      return { resp, store }
    }

    // Tie a store promise to the event lifetime so the SW stays alive until
    // the cache.put finishes; swallow its rejection (already tolerated inside
    // applyAppCodeStore, but the outer .catch guards the detached fallback).
    const keepStoreAlive = (store) => {
      const settled = store.catch(() => {})
      if (event && typeof event.waitUntil === 'function') event.waitUntil(settled)
    }

    const cached = await cache.match(cacheKey)

    // Cache-first whenever we have a versioned cached copy. A background
    // revalidate keeps the same version fresh, while an app update changes the
    // `?v=` cache key and naturally falls through to the network path below.
    if (shouldServeCacheFirst(!!cached)) {
      // Background-refresh, but tie the FETCH + STORE to the fetch event's
      // lifetime so the browser keeps the SW alive until both finish — a bare
      // detached promise can be cut off when the worker is terminated right
      // after the cached response returns, leaving stale code cached for the
      // next open. event may be absent in non-FetchEvent dispatch; the
      // .catch keeps a detached promise from surfacing an unhandled rejection.
      const refresh = revalidate()
        .then(({ store }) => store)
        .catch(() => {})  // rejects harmlessly when offline
      if (event && typeof event.waitUntil === 'function') event.waitUntil(refresh)
      return cached
    }

    // No cached copy yet: network. On success the body is served on THIS open +
    // cached for the next open. The store runs as its own event-bound promise
    // so a QuotaExceeded put can't discard this live response. A transient
    // SERVER error (5xx) must not blank a cached app if one appeared while we
    // were fetching — fall back to that copy for those (but NOT 4xx, which are
    // authoritative: a 404 means the app is gone, a 401/403 is real auth). On a
    // network REJECTION (offline / abort / DNS), the catch serves any late
    // cache fill too.
    try {
      const { resp, store } = await revalidate()
      keepStoreAlive(store)
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
    url.pathname === '/api/theme',
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
// `/api/apps/` is here too (not SWR): the drawer must show an install or
// delete immediately. Under SWR the list was always one fetch behind, so a
// just-installed app was missing and a just-deleted app lingered until the
// next refetch. NetworkFirst returns the live list when online and still
// falls back to the cached list offline (cold-drawer render preserved).
registerRoute(
  ({ url }) =>
    url.origin === self.location.origin &&
    (url.pathname === '/api/chats' || url.pathname === '/api/apps/'),
  new NetworkFirst({
    cacheName: 'mobius-shell-data',
    // KEPT at 5s deliberately. Workbox returns a cache fallback as a
    // successful fetch that TanStack can't distinguish from a confirmed live
    // response (isFetchedAfterMount), so a too-fast fallback of a stale `[]`
    // could wrongly trip Shell.jsx's auto-create-starter-chat. /api/chats does
    // NOT gate the visible shell render — the shell-nav route does (now 2s) —
    // so there's no load-speed reason to shorten this; the correctness risk
    // wins.
    networkTimeoutSeconds: 5,
  }),
)

// App frame/module — EVERY installed app (ungated; see appCodeStoreAction).
// Served by appCodeHandler: cache-first once cached, with background
// revalidation, so any app's second-and-later opens skip the network round
// trip entirely. The network attempt uses cache:'reload' to dodge the
// NetworkFirst+304 trap that once left the module uncached and blanked the
// in-shell iframe offline.
registerRoute(
  ({ url }) => isAppCodeRoute(url.pathname),
  appCodeHandler(OFFLINE_APPS_CACHE, { gated: false }),
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
// can never disagree. For the FULL shell, the cached (non-theme-injected)
// HTML renders correctly because Shell mounts useTheme(), which reapplies
// the persisted theme client-side after first paint.
//
// `/shell/embed/*` is the exception, and the second reason for this denylist.
// The embed branch (App.jsx) renders ChatEmbed OUTSIDE Shell. Before ChatEmbed
// gained its own useTheme() call it had NO client-side theme apply at all, so
// it depended entirely on the server-injected theme block in the served HTML —
// which the precached index.html does NOT carry. Serving the embed from the
// frozen precache stripped its theme (black-on-black / black composer). Even
// with ChatEmbed.useTheme() now reapplying client-side, the embed must still
// reach the server so its FIRST paint already carries the injected theme (no
// unthemed flash) and so it tracks server-side theme.css changes. So deny
// `/shell/embed/*` here and let it hit the network like `/recover`.
//
// `/recover*` is server-rendered (routes/recover.py) and is the safety net
// when the shell itself is broken — it MUST reach the network/server, never the
// precached shell. Without this denylist entry the installed PWA served the
// cached index.html for /recover, so the user landed on the shell instead of the
// recovery page (and recovery was unreachable exactly when it's needed most).

// Legacy instance-local mounts remain an extension point for installations
// that predate the reserved /services namespace. Stock Möbius ships none.
const PROXIED_APP_SUBTREES = []

// Owner-configured backend web services live under one reserved namespace.
// They are server-served, multi-page applications rather than SPA routes, so
// every depth below /services/ must reach the guarded backend proxy.  The
// concrete service map is private data (`/data/local-services.json`), never
// compiled into the stock shell or edited into this service worker.

registerRoute(new NavigationRoute(
  createHandlerBoundToURL('/index.html'),
  {
    denylist: [
      /^\/app-assets\//,
      /^\/app-embeds\//,
      /^\/apps\//,
      /^\/recover(\/|$)/,
      /^\/shell\/embed(\/|$)/,
      // Published Web Studio sites (/sites/<token>/...) are served by the
      // backend, NOT the SPA shell. Without this the root-scoped SW served the
      // cached index.html for a published URL, so opening it showed the Möbius
      // app instead of the built website.
      /^\/sites(\/|$)/,
      /^\/services(\/|$)/,
      // Legacy instance-local mounts remain supported as an extension point.
      ...PROXIED_APP_SUBTREES,
      /^\/(?!(?:shell|apps|recover)(?:\/|$))[A-Za-z0-9_-]+(?:\/(?:index\.html)?)?$/,
    ],
  },
))

// Standalone mini-app navigations: stored for offline-capable apps ONLY
// (gated) via the same reload-bypass handler — the standalone page carries
// the same ETag + `Cache-Control: no-cache`, so it had the identical 304-
// never-cached defect as the frame/module route. A non-capable app caches
// nothing → handler rethrows offline → catch handler serves the branded
// offline page. This gate is the offline-OPEN guarantee the manifest flag
// still owns; only the in-shell frame/module read path above is ungated.
registerRoute(
  ({ request, url }) =>
    request.mode === 'navigate' && url.pathname.startsWith('/apps/'),
  appCodeHandler(STANDALONE_APPS_CACHE, { gated: true }),
)

// Last resort for any document we still couldn't serve: the cached
// shell for /shell/*, the branded offline page for standalone +
// everything else. matchPrecache resolves the content-hashed entry.
setCatchHandler(async ({ request, url }) => {
  if (request.destination !== 'document') return Response.error()
  if (url.pathname.startsWith('/app-assets/')
      || url.pathname.startsWith('/app-embeds/')) {
    return (await matchPrecache('/offline.html')) || Response.error()
  }
  if (!url.pathname.startsWith('/apps/')) {
    return (
      (await matchPrecache('/index.html')) ||
      (await matchPrecache('/offline.html')) ||
      Response.error()
    )
  }
  return (await matchPrecache('/offline.html')) || Response.error()
})

// ── Precache warming ────────────────────────────────────────────
//
// Shell.jsx posts `moebius:precache-app` when an app is installed or updated
// (P1-B), and on shell load for pinned + recently-used apps (idle-scheduled;
// see lib/appPrecache.js), so frame + module are already cached before the
// user's first open — the open is then a pure cache read.
//
// Message payload:
//   { type: 'moebius:precache-app', frameUrl: string, moduleUrl: string }
//
// Key-normalization mirrors appCodeHandler exactly (strips token, _,
// install query params) so the warmed cache entries are found on the next
// cache.match(). Storage follows the UNGATED frame/module policy
// (appCodeStoreAction with gated=false): any 200 lands, matching the live
// open path. URLs are validated against isAppCodeRoute so a page message
// can only prime the frame/module routes, nothing else.

self.addEventListener('message', (event) => {
  const msg = event.data
  if (!msg || typeof msg !== 'object') return
  if (msg.type !== 'moebius:precache-app') return

  const { frameUrl, moduleUrl } = msg
  if (!frameUrl || !moduleUrl) return

  // Normalize a URL to its cache key (strip token/_ /install params).
  function normKey(rawUrl) {
    try {
      const u = new URL(rawUrl, self.location.origin)
      u.searchParams.delete('token')
      u.searchParams.delete('_')
      u.searchParams.delete('install')
      return u.href
    } catch {
      return null
    }
  }

  async function warmOne(rawUrl, cacheName) {
    const key = normKey(rawUrl)
    if (!key || !isAppCodeRoute(new URL(key).pathname)) return
    // Already cached? Skip redundant fetch.
    const cache = await caches.open(cacheName)
    const existing = await cache.match(key)
    if (existing) return
    const resp = await boundedFetch((signal) => {
      try {
        return new Request(rawUrl, { cache: 'reload', credentials: 'same-origin', signal })
      } catch {
        return new Request(rawUrl, { cache: 'reload', signal })
      }
    })
    // Same store path as the live open: tolerates a QuotaExceeded put (warming
    // is best-effort, never fail the install) and evicts the superseded `?v=`
    // versions of this route. Frame/module warming is always ungated.
    if (resp) await applyAppCodeStore(cache, key, resp, false)
  }

  // Tie the warming to the install event lifetime so the SW stays alive.
  // Falls back gracefully if event.waitUntil is unavailable.
  const work = (async () => {
    await warmOne(frameUrl, OFFLINE_APPS_CACHE)
    await warmOne(moduleUrl, OFFLINE_APPS_CACHE)
  })().catch(() => {})
  if (event && typeof event.waitUntil === 'function') event.waitUntil(work)
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
    if (/^https?:\/\//.test(raw)) {
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
      // For shell deep-links, navigate the existing client instead of only
      // postMessaging it. The message path is fast when the current Shell
      // listener is alive, but installed PWAs can have a stale/booting page
      // after a service-worker update; navigation gives the browser a durable
      // URL to load so actions like "Open Klix" don't focus Mobius and then
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
    if (clients.openWindow) return clients.openWindow(target)
  })())
})
