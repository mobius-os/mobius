/** Shared connectivity fixtures for browser specs. */
import { expect } from '@playwright/test'

/**
 * Take delivery genuinely offline: readiness and health probes fail, the
 * browser reports offline, and the call returns only once the shell shows
 * Offline. `reconnect()` then produces a real offline -> online delivery edge,
 * which is what re-wakes queued and outbox delivery. `alsoAbort` fails matching
 * requests too; `aborted()` counts them.
 */
export async function disconnectDelivery(page, { alsoAbort = () => false } = {}) {
  let aborted = 0
  const block = route => {
    const request = route.request()
    if (alsoAbort(request)) {
      aborted++
      return route.abort('internetdisconnected')
    }
    if (['/api/ready', '/api/health'].includes(new URL(request.url()).pathname)) {
      return route.abort('internetdisconnected')
    }
    return route.fallback()
  }
  await page.route('**/api/**', block)
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false })
    window.dispatchEvent(new Event('offline'))
  })
  const status = page.locator('.shell__connection-status')
  await expect(status).toContainText('Offline', { timeout: 15000 })
  return {
    status,
    aborted: () => aborted,
    reconnect: async () => {
      await page.unroute('**/api/**', block)
      await page.evaluate(() => {
        Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => true })
        window.dispatchEvent(new Event('online'))
      })
    },
  }
}
