// Tier 4b — window.mobius outbox. A write that fails (no network)
// queues in IndexedDB and flushes when the network returns. Needs a
// live mobius-test container so /mobius-runtime.js is served same-origin.
//
// We simulate the outage with route abort rather than context.setOffline
// so the queue→drain transition is deterministic: navigator.onLine stays
// true (so set() attempts the network and drain() is allowed to run),
// and the route controls whether the request succeeds.
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test('storage.set queues on failure and flushes on recovery', async ({ page }) => {
  let net = 'down'
  let puts = 0
  await page.route('**/api/storage/apps/**', (route) => {
    if (net === 'down') return route.abort()
    if (route.request().method() === 'PUT') puts += 1
    return route.fulfill({ status: 204, body: '' })
  })

  await page.goto(`${BASE}/shell/`)
  // Initialize the runtime against a throwaway app id with a stub token.
  await page.evaluate(async () => {
    const rt = await import('/mobius-runtime.js')
    rt.init({ appId: 990001, getToken: async () => 'stub-token' })
    // Start from an empty queue in case a previous run left entries.
    await new Promise((res) => { const r = indexedDB.deleteDatabase('mobius-outbox'); r.onsuccess = r.onerror = r.onblocked = () => res() })
  })

  // Network down → the write queues instead of failing.
  const queued = await page.evaluate(async () => {
    const r = await window.mobius.storage.set('t.json', { a: 1 })
    return { r, pending: await window.mobius.storage.pendingCount() }
  })
  expect(queued.r).toEqual({ queued: true })
  expect(queued.pending).toBe(1)

  // Network back → drain flushes the queue in order.
  net = 'up'
  await page.evaluate(() => window.mobius.storage._drain())
  await expect.poll(
    async () => page.evaluate(() => window.mobius.storage.pendingCount()),
    { timeout: 10000 },
  ).toBe(0)
  expect(puts).toBeGreaterThanOrEqual(1)
})
