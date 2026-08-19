/**
 * Bridge an outgoing cache-first shell worker to the current document.
 *
 * A worker that predates network-first navigation keeps looking up its own
 * revisioned index key. Replacing that response during the new worker's
 * install lets the next ordinary navigation load the current shell, without
 * activating the new worker underneath an open page.
 */
export async function findOutgoingShellEntries(cacheStorage, shellPath = '/index.html') {
  const entries = []
  for (const cacheName of await cacheStorage.keys()) {
    const cache = await cacheStorage.open(cacheName)
    for (const request of await cache.keys()) {
      try {
        if (new URL(request.url).pathname === shellPath) {
          entries.push({ cacheName, request })
        }
      } catch { /* ignore malformed third-party cache keys */ }
    }
  }
  return entries
}

export async function refreshOutgoingShellEntries(cacheStorage, entries, response) {
  if (!response?.ok || entries.length === 0) return
  await Promise.all(entries.map(async ({ cacheName, request }) => {
    const cache = await cacheStorage.open(cacheName)
    await cache.put(request, response.clone())
  }))
}
