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

const KEEP_RUNTIME_CACHES = new Set([
  VENDOR_CACHE,
  ESM_CACHE,
  OFFLINE_APPS_CACHE,
  STANDALONE_APPS_CACHE,
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
  if (/^mobius-(vendor|assets|apps|proxy|esm)-v\d+$/.test(name)) return true
  if (/^mobius-(vendor|esm)/.test(name)) return true
  if (/^mobius-(offline-apps|standalone)(?:-v\d+)?$/.test(name)) return true
  return false
}

// How long a page connectivity verdict (posted to the SW from useOnlineStatus's
// /api/health probe) is trusted before it's considered stale. Kept as a pure
// helper because older SW policies and future non-versioned routes may still
// need an explicit "known online" gate. Versioned offline-capable app code no
// longer uses this gate: if there is a cached copy, it is served immediately and
// refreshed in the background.
//
// This window was originally a two-sided knob for network-first vs cache-first
// app-code serving. The current versioned app-code policy no longer depends on
// it, but the helper stays conservative for any future route that needs a
// positive, recent online verdict.
export const VERDICT_MAX_AGE_MS = 60000

// PURE: is the device KNOWN to be online — i.e. is there a FRESH, POSITIVE page
// verdict? Exported pure so the truth table remains testable without a
// service-worker context. The current offline-capable app route does not need
// this to decide whether to serve cache-first because those URLs are versioned.
//
// @param {boolean|undefined} pageOnline last verdict (true/false/undefined)
// @param {number} verdictAt  Date.now() when the verdict was recorded (0 if none)
// @param {number} now        current Date.now()
// @param {number} maxAgeMs   freshness window (default VERDICT_MAX_AGE_MS)
export function isKnownOnline(pageOnline, verdictAt, now, maxAgeMs = VERDICT_MAX_AGE_MS) {
  return pageOnline === true && (now - verdictAt) < maxAgeMs
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
