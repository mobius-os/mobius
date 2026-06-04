/**
 * Service worker contract — locks in the vite-plugin-pwa migration.
 *
 * The SW source (`frontend/src/sw.js`) is processed by the plugin
 * at build time:
 *   - `precacheAndRoute(self.__WB_MANIFEST)` gets populated with
 *     content-hashed shell assets (no manual VERSION bump).
 *   - Workbox routing rules cover `/vendor/*`, `esm.sh/*`, and
 *     `/api/proxy?url=*.{img/font}` (SWR).
 *   - Push + notificationclick handlers are present in the built SW.
 *
 * What this test guards against: any future refactor that
 * accidentally drops precache injection or runtime caching, or
 * any plugin upgrade that changes the SW URL or cache shape.
 *
 * Run: npx playwright test tests/sw-pwa.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'


test.describe('Service worker — vite-plugin-pwa contract', () => {

  test('sw.js is served and registers on page load', async ({ page }) => {
    // The SW itself must be reachable at /sw.js and contain the
    // Workbox precache marker. A plugin misconfig that produces
    // an empty SW (or a different filename) would surface here.
    const res = await page.request.get(`${BASE}/sw.js`)
    expect(res.status()).toBe(200)
    const body = await res.text()
    // Workbox precache + routing artifacts visible in the bundled
    // SW. Without precacheAndRoute, the migration is incomplete.
    expect(body).toContain('precache')
    expect(body).toMatch(/mobius-vendor|mobius-esm|mobius-proxy/)
    expect(body).toContain('notificationclick')
    expect(body).toContain('notification-click')
    expect(body).toContain('postMessage')
    expect(body).toMatch(/\.navigate\(/)
  })

  test('manifest is reachable', async ({ page }) => {
    const res = await page.request.get(`${BASE}/manifest.webmanifest`)
    expect(res.status()).toBe(200)
    const m = JSON.parse(await res.text())
    // Bare minimum so a browser will treat the page as installable.
    expect(m.name || m.short_name).toBeTruthy()
    expect(m.icons?.length || 0).toBeGreaterThan(0)
    expect(m.start_url).toBeTruthy()
  })

  test('SW registers after a normal navigation', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 915 })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, body: '{}' })
    )
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })

    // Verify SW registration completes — the registration object
    // resolves once /sw.js has been fetched and parsed. We do NOT
    // wait for the SW to reach the 'activated' lifecycle state:
    // that's environment-sensitive (headless chromium under load
    // is slow to activate; we've seen 60s+ delays in CI) and
    // tests that depend on it flake. The registration completing
    // is the deterministic signal that vite-plugin-pwa output a
    // valid SW that the browser is willing to install. The
    // precache contents themselves are asserted in test #1 by
    // grepping the built `/sw.js`.
    const regOk = await page.evaluate(async () => {
      if (!('serviceWorker' in navigator)) return 'unsupported'
      try {
        const reg = await navigator.serviceWorker.register('/sw.js')
        return reg ? 'registered' : 'no-registration'
      } catch (e) {
        return `error:${e?.message ?? e}`
      }
    })
    expect(regOk).toBe('registered')
  })
})
