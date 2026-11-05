// Runtime-cache strategy pieces that sw.js registers, kept importable so tests
// drive them through Workbox's own request lifecycle.
import { CacheFirst } from 'workbox-strategies/CacheFirst.js'
import {
  entriesToTrim,
  isPinnableProxyResponse,
  PROXY_PINNED_CACHE,
  PROXY_PINNED_MAX_ENTRIES,
  withOpaqueFramePublicAssetCors,
} from './sw-cache-policy.js'

// Bounded-growth trim for APP_ASSETS_CACHE and the pinned proxy cache. Neither
// route carries a Workbox ExpirationPlugin (it's not a dep on these routes),
// and a single app can be ~19MB of assets, so without a cap the cache grows
// unbounded across installs and one asset-heavy app could evict the whole
// origin's quota. cacheDidUpdate fires after each successful write; we then
// trim the OLDEST entries (cache.keys() is insertion order) down to the
// cache's cap via the unit-tested entriesToTrim helper. Best-effort: a trim
// failure never affects the served response. Trimming removes a URL whatever
// its Vary headers, which proxied responses always carry.
export const trimCachePlugin = maxEntries => ({
  cacheDidUpdate: async ({ cacheName }) => {
    try {
      const cache = await caches.open(cacheName)
      const keys = (await cache.keys()).map(r => r.url)
      await Promise.all(
        entriesToTrim(keys, maxEntries)
          .map(k => cache.delete(k, { ignoreVary: true })),
      )
    } catch {
      // ignore — the cache is over its soft cap for one extra entry at worst.
    }
  },
})

// Commit-pinned proxied images (proxyCacheLane 'immutable'), cache-first.
//
// The server answers `Vary: Authorization`, and keeps doing so for every proxy
// read: the browser cache is shared by every bearer identity in this origin.
// This lane alone ignores Vary, so a renewed app token reuses the stored image.
// That is safe only here: the bytes are public and immutable, the cache is
// this origin's private, versioned cache, and logout deletes it.
export function pinnedProxyImageStrategy() {
  return new CacheFirst({
    cacheName: PROXY_PINNED_CACHE,
    matchOptions: { ignoreVary: true },
    // This cache is the one long-lived copy. Skipping the HTTP cache lets a
    // PROXY_PINNED_CACHE bump really refetch a changed downscaled rendition.
    fetchOptions: { cache: 'no-store' },
    plugins: [
      {
        cacheWillUpdate: async ({ response }) =>
          isPinnableProxyResponse(response) ? response : null,
        // Ignoring Vary also ignores `Vary: Origin`. An entry the shell stored
        // has no Access-Control-Allow-Origin, and an opaque app frame's CORS
        // read of it would fail; the bytes are public, so every hit allows it.
        cachedResponseWillBeUsed: async ({ cachedResponse }) =>
          withOpaqueFramePublicAssetCors(cachedResponse),
      },
      trimCachePlugin(PROXY_PINNED_MAX_ENTRIES),
    ],
  })
}
