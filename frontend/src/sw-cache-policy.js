/* Pure cache-policy helpers for the service worker.
 *
 * Extracted from sw.js so they can be unit-tested without a browser or
 * SW context — sw.js runs `self.skipWaiting()` at module load and can't
 * be imported in a plain test. These functions encode the rules that
 * stop (and undo) the cache-poisoning failure where a missing /vendor
 * file fell through to the SPA's `200 text/html`, got cached
 * cache-first, and was then served forever in place of the real module
 * ("failed to load dynamic module").
 */

// Current runtime cache names. Bumped to `-v2` ONCE to evict entries
// poisoned by the bug above. Do NOT bump again on routine deploys — the
// names are stable so vendor/esm aren't re-fetched every release.
export const VENDOR_CACHE = 'mobius-vendor-v2'
export const ESM_CACHE = 'mobius-esm-v2'
// Bumped -v2 → -v3 (2026-06-18): a one-time eviction of app-frame entries
// cached under the pre-fix, un-revved key (`?v=<updated_at>` with NO
// `-<frameRev>` suffix, because the SW-precached index.html lacked the
// mobius-frame-rev meta). Those stale frames carried the pre-injection
// app-frame.html (un-gated light @media flipping --bg on a light OS). Renaming
// the cache makes isStaleRuntimeCache delete the old `-v2` cache on the next
// activate so the next open re-warms under the correct revved key.
export const OFFLINE_APPS_CACHE = 'mobius-offline-apps-v3'
export const STANDALONE_APPS_CACHE = 'mobius-standalone-v2'
// app-assets bumped to -v2 ONCE (2026-06-12) to evict entries poisoned by
// ranged-request bodies: CubeRun's probe GET with `Range: bytes=0-0` came
// back from Chromium's HTTP cache as a status-200 response holding only the
// 1-byte slice, passed the status===200 check, and was stored under the
// bare index.html URL — every later open of the game served a one-character
// document (black screen). isRangeRequest below is the structural guard.
export const APP_ASSETS_CACHE = 'mobius-app-assets-v2'

// Bounded cap for APP_ASSETS_CACHE. A single packaged app can be ~19MB of
// models/textures (CubeRun); without a cap the immutable half grows without
// bound as apps are installed/re-installed, and one asset-heavy app could
// evict the whole origin's quota. There is no Workbox ExpirationPlugin on the
// /app-assets routes (they're plain CacheFirst/SWR with only a content-type
// guard), so the trim is manual: a documented entry-count ceiling enforced
// after each store via entriesToTrim() below (oldest-first FIFO — cache.keys()
// returns entries in insertion order). 600 entries is generous headroom for a
// handful of asset-heavy apps while still bounding a runaway; bump it here if a
// legitimately huge app needs more, rather than removing the ceiling.
export const APP_ASSETS_MAX_ENTRIES = 600

const KEEP_RUNTIME_CACHES = new Set([
  VENDOR_CACHE,
  ESM_CACHE,
  OFFLINE_APPS_CACHE,
  STANDALONE_APPS_CACHE,
  APP_ASSETS_CACHE,
])

// Content types we're willing to store in a cache-first asset cache.
// An SPA-fallback HTML body or an esm.sh `text/plain` error page is NOT
// in this list, so it's refused rather than cached.
export const CACHEABLE_ASSET_TYPES = [
  'application/javascript',
  'text/javascript',
  'text/css',
  'application/wasm',
  'application/json',
  'application/octet-stream',
]

// True when a response is safe to store in /vendor or esm.sh caches.
// Refuses non-200 and any non-asset content type — the structural fix
// for the poisoning class.
export function isCacheableAssetResponse(response) {
  if (!response || response.status !== 200) return false
  const ct = (response.headers.get('content-type') || '').toLowerCase()
  return CACHEABLE_ASSET_TYPES.some(t => ct.includes(t))
}

// Public assets executed by an opaque app frame or its nested chat embed. Keep
// this exact: the app imports the runtime + vendor modules, while the inherited
// Origin:null chat document loads the Vite shell JS/CSS under /assets. The rest
// of the shell stays on the ordinary same-origin policy.
export function isOpaqueFramePublicAssetPath(pathname) {
  return pathname === '/mobius-runtime.js'
    || pathname.startsWith('/vendor/')
    || pathname.startsWith('/assets/')
}

// The shell service worker precaches these modules itself (without an Origin
// request header), so an older cache may contain a valid response without
// Access-Control-Allow-Origin. When that cached response is handed to the
// opaque frame Chromium applies CORS and rejects it before the app can mount.
// Decorate both old cache hits and fresh fetches at the worker boundary; the
// server emits the same wildcard header so direct and HTTP-cached responses
// have an identical contract.
export function withOpaqueFramePublicAssetCors(response) {
  if (!response) return response
  if (response.headers.get('access-control-allow-origin') === '*') {
    return response
  }
  const headers = new Headers(response.headers)
  headers.set('Access-Control-Allow-Origin', '*')
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  })
}

// True when a cache should be deleted on activate. Keeps the current v2
// runtime caches; deletes (a) the legacy hand-written `mobius-*-vN`
// caches and (b) any other stale `mobius-(vendor|esm)*` — notably the
// poisoned un-suffixed `mobius-vendor` / `mobius-esm` left on installs
// that hit the old bug. The KEEP check MUST come first: `mobius-vendor-v2`
// also matches the legacy `-v\d+$` pattern and would otherwise be
// deleted every activate, re-fetching vendor on every deploy.
// Maintenance trap: the legacy regex below matches `mobius-proxy-v\d+`. The
// live proxy cache is currently un-versioned (`mobius-proxy`, kept), but if
// it's ever versioned, add it to KEEP_RUNTIME_CACHES or it'll be evicted
// every activate (re-fetching all proxied assets on every deploy).
export function isStaleRuntimeCache(name) {
  if (KEEP_RUNTIME_CACHES.has(name)) return false
  if (/^mobius-(vendor|assets|app-assets|apps|proxy|esm)-v\d+$/.test(name)) return true
  if (/^mobius-(vendor|esm)/.test(name)) return true
  if (/^mobius-(offline-apps|standalone)(?:-v\d+)?$/.test(name)) return true
  return false
}

// PURE: does this request ask for a byte sub-range? Ranged requests must
// bypass the app-assets caches entirely (neither served from cache nor
// stored). Two failure modes force the REQUEST-side check:
//   - storing: Chromium can satisfy a ranged fetch from its HTTP cache as a
//     STATUS-200 response whose body is just the requested slice — there is
//     no response-side marker (no 206, no Content-Range) distinguishing it
//     from a full body, so a cacheWillUpdate status check cannot catch it.
//     Cached under the bare URL it truncates the asset for every later
//     consumer (the 2026-06-12 CubeRun black-screen outage).
//   - matching: cache.match ignores request headers, so a cached full body
//     would be returned uncut to a ranged request (harmless for probes,
//     wrong for media seeking).
export function isRangeRequest(request) {
  return !!request && !!request.headers && request.headers.has('range')
}

// Both namespaces address the same packaged files. /app-assets remains the
// ordinary protected URL; /app-embeds is the frameable, response-sandboxed
// document lane used below an opaque app frame.
export function isPackagedAppAsset(pathname) {
  return pathname.startsWith('/app-assets/') || pathname.startsWith('/app-embeds/')
}

// Entry documents and script-level fetches keep their /app-embeds identity so
// a response-sandboxed document can never be stored on the ordinary protected
// lane. Only actual, SW-controlled browser subresource requests share the
// /app-assets/by-id cache key. (A response-sandboxed opaque child is not itself
// controlled by the shell worker, so its own relative requests use the browser
// HTTP cache rather than this normalization.)
export function packagedAppAssetCacheKey(
  rawUrl,
  { isDocument = false, isSubresource = false } = {},
) {
  let url
  try { url = new URL(rawUrl) } catch { return rawUrl }
  if (isSubresource && !isDocument) {
    url.pathname = url.pathname.replace(
      /^\/app-embeds\/by-id\/(\d+)\//,
      '/app-assets/by-id/$1/',
    )
  }
  return url.href
}

// PURE: is a packaged-app static asset (/app-assets/{slug}/...) immutable?
// Mirrors the server's _HASHED_ASSET_NAME (backend/app/main.py): a
// content-hash segment in the FILENAME ([.-]<8+ hex>.<ext>) means a
// re-install that changes the bytes also changes the name, so the URL is
// the validator — the server sends `Cache-Control: ... immutable` and the
// SW may cache it forever. Keep the regex in sync with the backend.
//
// The hex run must contain at least one ALPHA hex char (a-f). An all-digit
// run is NOT a content hash — it's a version number or a timestamp
// (`main.12345678.js`, `app.20260612.js`), and those names ARE reused
// across re-installs. Treating them as immutable would cache-first a stale
// build forever (nothing busts an /app-assets URL once cached, and the
// server would send `immutable` so the browser never revalidates either).
// A real digest contains a-f with overwhelming probability, so the alpha
// requirement keeps true hashes in while letting decimal versions
// revalidate. The lookahead bounds the run to 8+ hex chars; the body then
// requires one a-f somewhere inside it.
const HASHED_ASSET_NAME = /[.-](?=[0-9a-f]{8,}\.)[0-9a-f]*[a-f][0-9a-f]*\./i

export function isImmutableAppAsset(pathname) {
  if (!isPackagedAppAsset(pathname)) return false
  const name = pathname.slice(pathname.lastIndexOf('/') + 1)
  return HASHED_ASSET_NAME.test(name)
}

// True when a response may enter the immutable half of the app-assets
// cache. App assets span arbitrary content types (models, textures,
// fonts, audio), so unlike CACHEABLE_ASSET_TYPES this is a denylist:
// refuse non-200 and text/html — a hashed filename is never a document,
// so an HTML body at one can only be a fallback/error page (the
// poisoning class isCacheableAssetResponse exists for).
export function isCacheableAppAssetResponse(response) {
  if (!response || response.status !== 200) return false
  const ct = (response.headers.get('content-type') || '').toLowerCase()
  return !ct.includes('text/html')
}

export function hasOpaqueEmbedSandbox(response) {
  if (!response || response.status !== 200) return false
  const csp = (response.headers.get('content-security-policy') || '').toLowerCase()
  return /(?:^|;)\s*sandbox(?:\s|;|$)/.test(csp)
    && !csp.includes('allow-same-origin')
}

export function isCacheableOpaqueEmbedDocument(response) {
  if (!hasOpaqueEmbedSandbox(response)) return false
  const type = (response.headers.get('content-type') || '').toLowerCase()
  return type.includes('text/html')
}

// PURE: should appCodeHandler serve the CACHED copy first (instant) vs go
// to network? If the route is cached, serve it immediately and refresh in the
// background. Freshness comes from the versioned frame/module URL (`?v=` is part
// of the cache key): an app update changes the key and naturally becomes a cache
// miss, so the updated app still goes to the network on its first open. No
// cached copy → always network (cold path, nothing to serve).
export function shouldServeCacheFirst(hasCached) {
  return !!hasCached
}

// PURE: the per-app code routes the SW serves cache-first — the iframe runtime
// frame and the compiled module. Kept in lockstep with the registerRoute
// matcher in sw.js and the server routes in backend/app/routes/apps.py.
export function isAppCodeRoute(pathname) {
  return /^\/api\/apps\/\d+\/(frame|module)$/.test(pathname)
}

// PURE: what to do with a RESOLVED network response on an app-code route.
// Two cache surfaces share the handler but differ on who may be stored:
//   - frame/module reads (gated=false): store EVERY 200. Loading speed must
//     not depend on the manifest's offline_capable flag — the flag still
//     gates offline WRITE semantics and the standalone-navigation guarantee,
//     but a cached read path (instant open + background revalidate) is safe
//     for every installed app because freshness comes from the `?v=` cache
//     key + the background refresh, not from the flag.
//   - standalone navigations (gated=true): store only responses the server
//     marks `X-Mobius-Offline: 1`; a 200 WITHOUT the header purges the entry
//     so an app toggled offline_capable OFF self-heals on the next refresh
//     (cache.put is otherwise never called for it, so the stale body would
//     persist forever).
// Non-200s (304s, 4xx, 5xx) are never stored — 'ignore' leaves any existing
// entry untouched; shouldFallBackToCacheOnError decides what the page sees.
export function appCodeStoreAction(status, offlineHeader, gated) {
  if (status !== 200) return 'ignore'
  if (!gated) return 'store'
  return offlineHeader === '1' ? 'store' : 'purge'
}

// PURE: on the network path, should a network RESPONSE be replaced by the cached
// copy? Yes for a SERVER error (>=500) when we have a cached app — a transient
// backend 500/502/503 must not blank a known-good offline-capable app that we
// have cached. NOT for 4xx (404 app-deleted / 401-403 auth are authoritative —
// masking them with a stale cached app would hide a real state), and NOT for
// 2xx/3xx (the real response). Network REJECTIONS (offline / DNS / abort) are
// handled separately by the handler's catch; this is only for resolved-but-
// failed HTTP responses. Exported pure so the rule is unit-tested.
export function shouldFallBackToCacheOnError(status, hasCached) {
  return hasCached && status >= 500
}

// PURE: given the cache key just stored (`storedKey`) and the cache's
// current key list (`existingKeys`), return the SUPERSEDED keys to evict —
// the ones that address the SAME app-code route (same origin + same
// `/api/apps/<id>/<frame|module>` pathname) but carry a DIFFERENT `?v=`
// version than the one we just stored.
//
// Why this is precise (not a heuristic): AppCanvas pins the frame/module
// URL with `?v=<app.updated_at>`, and that `?v=` is part of the cache key
// (token/`_`/`install` are stripped, but `v` is kept — it's the freshness
// discriminator). So every edit of an app leaves a fresh entry behind under
// a new `?v=` while the OLD versioned entry lingers, unreachable forever
// (its `?v=` will never be requested again) — unbounded growth of one
// frame/module pair per edit. Matching on pathname + a differing `v`
// evicts exactly the prior versions of THIS route and nothing else: a
// different app id, a different route type (frame vs module), or the
// just-stored key itself are all left untouched.
//
// Robust to keys that don't parse as URLs (returned as-is from cache.keys()
// in odd engines) — those are simply skipped rather than throwing.
export function supersededVersionKeys(storedKey, existingKeys) {
  let stored
  try {
    stored = new URL(storedKey)
  } catch {
    return []
  }
  const storedV = stored.searchParams.get('v')
  const out = []
  for (const k of existingKeys || []) {
    if (k === storedKey) continue
    let u
    try {
      u = new URL(k)
    } catch {
      continue
    }
    if (u.origin !== stored.origin) continue
    if (u.pathname !== stored.pathname) continue
    // Same route, different version → a superseded prior edit. (A null `v`
    // on either side still counts as "different" when the other has one,
    // so an un-versioned legacy entry is also cleaned up.)
    if (u.searchParams.get('v') !== storedV) out.push(k)
  }
  return out
}

// PURE: given the cache's current key list (`existingKeys`, in cache.keys()
// insertion order — oldest first) and a `max` entry ceiling, return the
// OLDEST keys to delete so the count drops to `max`. Empty when already at or
// under the ceiling. This is the manual LRU/FIFO trim for APP_ASSETS_CACHE
// (see APP_ASSETS_MAX_ENTRIES) — keep the cache bounded without depending on
// Workbox's ExpirationPlugin, which isn't wired onto the /app-assets routes.
// FIFO (not true LRU) is acceptable here: immutable hashed assets are stable
// per release and a re-install ships new names, so eviction order rarely
// matters and oldest-first is the cheap, predictable choice.
export function entriesToTrim(existingKeys, max) {
  const keys = existingKeys || []
  if (!(max > 0) || keys.length <= max) return []
  return keys.slice(0, keys.length - max)
}
