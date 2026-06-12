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
export const OFFLINE_APPS_CACHE = 'mobius-offline-apps-v2'
export const STANDALONE_APPS_CACHE = 'mobius-standalone-v2'
// app-assets bumped to -v2 ONCE (2026-06-12) to evict entries poisoned by
// ranged-request bodies: CubeRun's probe GET with `Range: bytes=0-0` came
// back from Chromium's HTTP cache as a status-200 response holding only the
// 1-byte slice, passed the status===200 check, and was stored under the
// bare index.html URL — every later open of the game served a one-character
// document (black screen). isRangeRequest below is the structural guard.
export const APP_ASSETS_CACHE = 'mobius-app-assets-v2'

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

// PURE: is a packaged-app static asset (/app-assets/{slug}/...) immutable?
// Mirrors the server's _HASHED_ASSET_NAME (backend/app/main.py): a
// content-hash segment in the FILENAME ([.-]<8+ hex>.<ext>) means a
// re-install that changes the bytes also changes the name, so the URL is
// the validator — the server sends `Cache-Control: ... immutable` and the
// SW may cache it forever. Keep the regex in sync with the backend.
const HASHED_ASSET_NAME = /[.-][0-9a-f]{8,}\./i

export function isImmutableAppAsset(pathname) {
  if (!pathname.startsWith('/app-assets/')) return false
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

// PURE: should offlineCapableHandler serve the CACHED copy first (instant) vs go
// to network? If the route is cached, serve it immediately and refresh in the
// background. Freshness comes from the versioned frame/module URL (`?v=` is part
// of the cache key): an app update changes the key and naturally becomes a cache
// miss, so the updated app still goes to the network on its first open. No
// cached copy → always network (cold path, nothing to serve).
export function shouldServeCacheFirst(hasCached) {
  return !!hasCached
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
