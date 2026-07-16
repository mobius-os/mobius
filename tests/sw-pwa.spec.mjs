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
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/sw-pwa.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function ownerToken(page) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  return page.evaluate(() => localStorage.getItem('token'))
}


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

  test('controlled SW serves sandboxed app-embed documents, never shell HTML', async ({ page, request, context, browser }) => {
    const token = await ownerToken(page)
    const headers = { Authorization: `Bearer ${token}` }
    const created = await request.post(`${BASE}/api/apps/`, {
      headers,
      data: {
        name: `Opaque static SW ${Date.now()}`,
        description: 'Disposable controlled-service-worker fixture.',
        jsx_source: 'export default function App(){return <main>fixture</main>}',
      },
    })
    expect(created.status()).toBe(201)
    const app = await created.json()
    const prefix = `apps/${app.slug}/static`
    const write = (path, body) => request.put(
      `${BASE}/api/fs/write?path=${encodeURIComponent(`${prefix}/${path}`)}`,
      { headers: { ...headers, 'Content-Type': 'text/plain' }, data: body },
    )
    try {
      expect((await write('index.html', `<!doctype html><title>Opaque packaged fixture</title><script src="./child.deadbeef.js"></script><main id="packaged">real packaged document</main>`)).ok()).toBeTruthy()
      expect((await write('child.deadbeef.js', `parent.postMessage({type:'opaque-static-sw-ready',origin:location.origin},'*')`)).ok()).toBeTruthy()
      expect((await write('hostile.svg', `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><script><![CDATA[
        (async()=>{let token=null;try{token=localStorage.getItem('token')}catch(_e){}
        let api=-1;try{api=(await fetch('/api/apps/',token?{headers:{Authorization:'Bearer '+token}}:{})).status}catch(_e){}
        parent.postMessage({type:'opaque-svg-proof',origin:location.origin,token,api},'*')})()
      ]]></script><rect width="20" height="20" fill="red"/></svg>`)).ok()).toBeTruthy()

      await page.evaluate(async () => {
        await navigator.serviceWorker.register('/sw.js')
        await navigator.serviceWorker.ready
      })
      if (!await page.evaluate(() => !!navigator.serviceWorker.controller)) {
        await page.reload({ waitUntil: 'domcontentloaded' })
      }
      await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller))
        .toBe(true)

      const alias = `${BASE}/app-embeds/by-id/${app.id}/index.html?v=sw-contract`
      await page.evaluate((src) => {
        window.__opaqueStaticMessages = []
        window.addEventListener('message', event => {
          if (event.data?.type === 'opaque-static-sw-ready') {
            window.__opaqueStaticMessages.push(event.data)
          }
        })
        const frame = document.createElement('iframe')
        frame.id = 'opaque-static-fixture'
        frame.src = src
        document.body.appendChild(frame)
      }, alias)
      await expect.poll(() => page.evaluate(() => window.__opaqueStaticMessages)).toEqual([
        { type: 'opaque-static-sw-ready', origin: 'null' },
      ])
      let child = page.frames().find(frame => frame.url().includes('/app-embeds/by-id/'))
      expect(child).toBeTruthy()
      expect(await child.title()).toBe('Opaque packaged fixture')
      await expect(child.locator('#packaged')).toHaveText('real packaged document')

      const keys = await page.evaluate(async () => {
        const cache = await caches.open('mobius-app-assets-v2')
        return (await cache.keys()).map(request => new URL(request.url).pathname)
      })
      expect(keys).toContain(`/app-embeds/by-id/${app.id}/index.html`)
      expect(keys).toContain(`/app-assets/by-id/${app.id}/child.deadbeef.js`)
      expect(keys).not.toContain(`/app-embeds/by-id/${app.id}/child.deadbeef.js`)

      const hostileContext = await browser.newContext({ serviceWorkers: 'block' })
      try {
        const hostile = await hostileContext.newPage()
        await hostile.goto(BASE, { waitUntil: 'domcontentloaded' })
        await hostile.evaluate(ownerToken => localStorage.setItem('token', ownerToken), token)
        const hostileUrl = new URL(BASE)
        hostileUrl.hostname = hostileUrl.hostname === '127.0.0.1' ? 'localhost' : '127.0.0.1'
        await hostile.goto(`${hostileUrl.origin}/offline.html`, { waitUntil: 'domcontentloaded' })
        await hostile.evaluate((src) => {
          window.__opaqueSvgProof = null
          window.addEventListener('message', event => {
            if (event.data?.type === 'opaque-svg-proof') {
              window.__opaqueSvgProof = { ...event.data, eventOrigin: event.origin }
            }
          })
          const frame = document.createElement('iframe')
          frame.src = src
          document.body.appendChild(frame)
        }, `${BASE}/app-embeds/by-id/${app.id}/hostile.svg`)
        await expect.poll(() => hostile.evaluate(() => window.__opaqueSvgProof)).toEqual({
          type: 'opaque-svg-proof', origin: 'null', eventOrigin: 'null',
          token: null, api: 401,
        })
      } finally {
        const seeded = hostileContext.pages()[0]
        if (seeded) {
          await seeded.goto(BASE, { waitUntil: 'domcontentloaded' }).catch(() => {})
          await seeded.evaluate(() => localStorage.removeItem('token')).catch(() => {})
        }
        await hostileContext.close()
      }

      // Recreate the frame while offline and already controlled. A mistaken
      // NavigationRoute match would return precached /index.html here.
      await context.setOffline(true)
      await page.evaluate(() => {
        window.__opaqueStaticMessages = []
        document.getElementById('opaque-static-fixture')?.remove()
      })
      await page.evaluate((src) => {
        const frame = document.createElement('iframe')
        frame.id = 'opaque-static-fixture'
        frame.src = src
        document.body.appendChild(frame)
      }, alias)
      await expect.poll(() => page.evaluate(() => window.__opaqueStaticMessages)).toEqual([
        { type: 'opaque-static-sw-ready', origin: 'null' },
      ])
      child = page.frames().find(frame => frame.url().includes('/app-embeds/by-id/'))
      expect(await child.title()).toBe('Opaque packaged fixture')
      await expect(child.locator('#packaged')).toHaveText('real packaged document')
    } finally {
      await context.setOffline(false)
      await request.delete(`${BASE}/api/apps/${app.id}`, {
        headers, failOnStatusCode: false,
      })
    }
  })
})
