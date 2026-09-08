// Require live list evidence while retaining successful responses for offline viewing.
import { Strategy } from 'workbox-strategies/Strategy.js'

export class LiveShellList extends Strategy {
  async _handle(request, handler) {
    const response = await handler.fetch(request)
    request.signal.throwIfAborted()
    // CacheStorage is an optional offline copy, not a condition of live success.
    // Workbox owns response eligibility and extends the fetch event for the write.
    handler.waitUntil(handler.cachePut(request, response.clone()).catch(() => false))
    return response
  }
}
