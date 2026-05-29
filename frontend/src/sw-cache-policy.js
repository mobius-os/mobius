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

const KEEP_RUNTIME_CACHES = new Set([VENDOR_CACHE, ESM_CACHE])

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
export function isStaleRuntimeCache(name) {
  if (KEEP_RUNTIME_CACHES.has(name)) return false
  if (/^mobius-(vendor|assets|apps|proxy|esm)-v\d+$/.test(name)) return true
  if (/^mobius-(vendor|esm)/.test(name)) return true
  return false
}
