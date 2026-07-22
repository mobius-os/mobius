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
const HOSTILE_ORIGIN = process.env.MOBIUS_TEST_HOSTILE_ORIGIN || null

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

  test('retained PDF and KaTeX assets work before their first online use', async ({ page, context }) => {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.evaluate(async () => {
      await navigator.serviceWorker.register('/sw.js')
      await navigator.serviceWorker.ready
    })
    await expect.poll(() => page.evaluate(
      () => !!navigator.serviceWorker.controller,
    )).toBe(true)

    const paths = [
      '/vendor/pdfjs/pdf.worker.mjs',
      '/vendor/katex/katex.min.css',
      '/vendor/katex/fonts/KaTeX_Main-Regular.woff2',
      '/vendor/katex@0.17.0/katex.min.css',
      '/vendor/katex@0.17.0/fonts/KaTeX_Main-Regular.woff2',
    ]

    await context.setOffline(true)
    try {
      const results = await page.evaluate(async (urls) => Promise.all(
        urls.map(async (url) => {
          try {
            const response = await fetch(url)
            return {
              url,
              status: response.status,
              bytes: (await response.arrayBuffer()).byteLength,
            }
          } catch (error) {
            return { url, error: error?.name || String(error) }
          }
        }),
      ), paths)
      expect(results).toEqual(paths.map(url => expect.objectContaining({
        url,
        status: 200,
        bytes: expect.any(Number),
      })))
      for (const result of results) expect(result.bytes).toBeGreaterThan(1_000)
    } finally {
      await context.setOffline(false)
    }
  })

  test('standalone first reopen serves an update applied while the app was closed', async ({ page, request, context }) => {
    const token = await ownerToken(page)
    const headers = { Authorization: `Bearer ${token}` }
    const stamp = Date.now()
    const firstMarker = `standalone revision one ${stamp}`
    const secondMarker = `standalone revision two ${stamp}`
    const source = marker => (
      `export default function App(){return <main id="standalone-revision">${marker}</main>}`
    )
    const created = await request.post(`${BASE}/api/apps/`, {
      headers,
      data: {
        name: `Standalone freshness ${stamp}`,
        description: 'Disposable standalone update-cache fixture.',
        offline_capable: true,
        jsx_source: source(firstMarker),
      },
    })
    expect(created.status()).toBe(201)
    const app = await created.json()
    const standaloneUrl = `${BASE}/apps/${app.slug}/`

    try {
      await page.evaluate(async () => {
        await navigator.serviceWorker.register('/sw.js')
        await navigator.serviceWorker.ready
      })
      if (!await page.evaluate(() => !!navigator.serviceWorker.controller)) {
        await page.reload({ waitUntil: 'domcontentloaded' })
      }
      await expect.poll(() => page.evaluate(
        () => !!navigator.serviceWorker.controller,
      )).toBe(true)

      await page.goto(standaloneUrl, { waitUntil: 'domcontentloaded' })
      await expect(page.locator('#standalone-revision')).toHaveText(firstMarker)
      await expect.poll(() => page.evaluate(async url => {
        const cache = await caches.open('mobius-standalone-v2')
        return !!await cache.match(url)
      }, standaloneUrl)).toBe(true)

      // Close the standalone page before applying the edit, so its SSE cannot
      // observe app_updated and mask a stale-navigation-cache regression.
      await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
      const updated = await request.patch(`${BASE}/api/apps/${app.id}`, {
        headers,
        data: { jsx_source: source(secondMarker) },
      })
      expect(updated.ok()).toBeTruthy()

      // This FIRST navigation after the edit must be authoritative. A
      // cache-first standalone route serves revision one here and only refreshes
      // the cache in the background, forcing a second reload to see the update.
      await page.goto(standaloneUrl, { waitUntil: 'domcontentloaded' })
      await expect(page.locator('#standalone-revision')).toHaveText(secondMarker)

      // The same authoritative response is now the offline fallback.
      await context.setOffline(true)
      await page.reload({ waitUntil: 'domcontentloaded' })
      await expect(page.locator('#standalone-revision')).toHaveText(secondMarker)
    } finally {
      await context.setOffline(false)
      await request.delete(`${BASE}/api/apps/${app.id}`, {
        headers, failOnStatusCode: false,
      })
    }
  })

  test('opaque app frames load their cached module through the parent while offline', async ({ page, request, context }) => {
    const runtimeErrors = []
    const sanitize = value => String(value || '')
      .replace(/([?&]token=)[^&\s"'<>]+/gi, '$1[redacted]')
    page.on('console', message => {
      if (['error', 'warning'].includes(message.type())) {
        runtimeErrors.push(`${message.type()}: ${sanitize(message.text())}`)
      }
    })
    page.on('pageerror', error => runtimeErrors.push(`pageerror: ${sanitize(error.message)}`))
    const token = await ownerToken(page)
    const headers = { Authorization: `Bearer ${token}` }
    const marker = `offline module broker ${Date.now()}`
    const created = await request.post(`${BASE}/api/apps/`, {
      headers,
      data: {
        name: `Offline broker ${Date.now()}`,
        description: 'Disposable opaque-frame offline module fixture.',
        offline_capable: true,
        jsx_source: `export default function App(){return <main id="offline-module-marker">${marker}</main>}`,
      },
    })
    expect(created.status()).toBe(201)
    const app = await created.json()

    try {
      await page.evaluate(async () => {
        await navigator.serviceWorker.register('/sw.js')
        await navigator.serviceWorker.ready
      })
      if (!await page.evaluate(() => !!navigator.serviceWorker.controller)) {
        await page.reload({ waitUntil: 'domcontentloaded' })
      }
      await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller))
        .toBe(true)

      // Enter through the supported cold-start URL. The contract under test is
      // shell -> AppCanvas -> opaque frame -> parent module broker; coupling it
      // to the drawer row's new-item entrance animation made the browser wait
      // for actionability even though background warming had already succeeded.
      await page.goto(`${BASE}/shell/?app=${app.id}`, {
        waitUntil: 'domcontentloaded',
      })
      const frameSelector = `iframe[src*="/api/apps/${app.id}/frame"]`
      await page.waitForSelector(frameSelector)
      let child
      await expect.poll(async () => {
        const element = await page.locator(frameSelector).elementHandle()
        child = await element?.contentFrame()
        return !!child
      }).toBe(true)
      await expect(child.locator('#offline-module-marker')).toHaveText(marker)
      expect(await child.evaluate(() => {
        try {
          return !('serviceWorker' in navigator) || !navigator.serviceWorker.controller
        } catch (error) {
          return error instanceof DOMException && error.name === 'SecurityError'
        }
      })).toBe(true)

      const appVersion = String(app.updated_at ?? '0').trim() || '0'
      const frameRev = await page.locator('meta[name="mobius-frame-rev"]').getAttribute('content')
      const frameVersion = frameRev ? `${appVersion}-${frameRev}` : appVersion
      await expect.poll(() => page.evaluate(async ({ appId, appVersion, frameVersion }) => {
        const cache = await caches.open('mobius-offline-apps-v4')
        const keys = await cache.keys()
        const has = (route, version) => keys.some(request => {
          const url = new URL(request.url)
          return url.pathname === `/api/apps/${appId}/${route}`
            && url.searchParams.get('v') === String(version)
        })
        return {
          frame: has('frame', frameVersion),
          module: has('module', appVersion),
        }
      }, { appId: app.id, appVersion, frameVersion })).toEqual({
        frame: true,
        module: true,
      })

      await context.setOffline(true)
      await page.reload({ waitUntil: 'domcontentloaded' })
      try {
        await expect.poll(async () => {
          child = page.frames().find(
            frame => frame.url().includes(`/api/apps/${app.id}/frame`),
          )
          if (!child) return null
          return child.locator('#offline-module-marker').textContent().catch(() => null)
        }, { timeout: 15_000 }).toBe(marker)
      } catch (error) {
        const frames = await Promise.all(page.frames().map(async frame => ({
          url: frame.url(),
          body: await frame.locator('body').innerText({ timeout: 500 }).catch(() => null),
          errorPanel: await frame.locator('#error-panel')
            .getAttribute('class', { timeout: 500 }).catch(() => null),
          runtime: await frame.evaluate(() => {
            let serviceWorker = 'unavailable'
            try {
              serviceWorker = navigator.serviceWorker?.controller ? 'controlled' : 'uncontrolled'
            } catch (error) {
              serviceWorker = error?.name || 'blocked'
            }
            return {
              readyState: document.readyState,
              locationOrigin: window.location.origin,
              selfOrigin: window.origin,
              appId: globalThis._FRAME_APP_ID,
              rootChildren: document.getElementById('root')?.childElementCount ?? null,
              scripts: document.scripts.length,
              serviceWorker,
            }
          }).catch(() => null),
        })))
        throw new Error(
          `${error.message}\nOffline frame state: ${JSON.stringify(frames)}`
          + `\nRuntime errors: ${JSON.stringify(runtimeErrors.slice(-20))}`,
        )
      }
      await expect(child.locator('#error-panel')).not.toHaveClass(/visible/)
    } finally {
      await context.setOffline(false)
      await request.delete(`${BASE}/api/apps/${app.id}`, {
        headers, failOnStatusCode: false,
      })
    }
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
      expect((await write('child.deadbeef.js', `(async()=>{
        let token=null;try{token=localStorage.getItem('token')}catch(_e){}
        let parentToken=null;try{parentToken=parent.localStorage.getItem('token')}catch(_e){}
        let api=-1;try{api=(await fetch('/api/apps/',token?{headers:{Authorization:'Bearer '+token}}:{})).status}catch(_e){}
        parent.postMessage({type:'opaque-static-sw-ready',origin:self.origin,token,parentToken,api},'*')
      })()`)).ok()).toBeTruthy()
      expect((await write('hostile.svg', `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><script><![CDATA[
        (async()=>{let token=null;try{token=localStorage.getItem('token')}catch(_e){}
        let api=-1;try{api=(await fetch('/api/apps/',token?{headers:{Authorization:'Bearer '+token}}:{})).status}catch(_e){}
        parent.postMessage({type:'opaque-svg-proof',origin:self.origin,token,api},'*')})()
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
      const controlledCsp = await page.evaluate(async src => {
        const response = await fetch(src, { cache: 'reload' })
        return response.headers.get('content-security-policy')
      }, alias)
      expect(controlledCsp).toContain('sandbox')
      expect(controlledCsp).not.toContain('allow-same-origin')
      await page.evaluate((src) => {
        window.__opaqueStaticMessages = []
        window.addEventListener('message', event => {
          if (event.data?.type === 'opaque-static-sw-ready') {
            window.__opaqueStaticMessages.push({ ...event.data, eventOrigin: event.origin })
          }
        })
        const frame = document.createElement('iframe')
        frame.id = 'opaque-static-fixture'
        frame.src = src
        document.body.appendChild(frame)
      }, alias)
      await expect.poll(() => page.evaluate(() => window.__opaqueStaticMessages)).toEqual([
        {
          type: 'opaque-static-sw-ready', origin: 'null', eventOrigin: 'null',
          token: null, parentToken: null, api: 401,
        },
      ])
      let child = page.frames().find(frame => frame.url().includes('/app-embeds/by-id/'))
      expect(child).toBeTruthy()
      expect(await child.title()).toBe('Opaque packaged fixture')
      await expect(child.locator('#packaged')).toHaveText('real packaged document')
      expect(await page.locator('#opaque-static-fixture').evaluate(frame => {
        try {
          void frame.contentWindow.document
          return 'accessible'
        } catch (error) {
          return error?.name || 'denied'
        }
      })).toBe('SecurityError')

      const keys = await page.evaluate(async () => {
        const cache = await caches.open('mobius-app-assets-v2')
        return (await cache.keys()).map(request => new URL(request.url).pathname)
      })
      expect(keys).toContain(`/app-embeds/by-id/${app.id}/index.html`)
      expect(keys).not.toContain(`/app-assets/by-id/${app.id}/index.html`)
      // A response-sandboxed child has an opaque effective origin and is not
      // controlled by the shell worker. Its immutable subresources therefore
      // use Chromium's HTTP cache; they must not create a duplicate SW entry.
      expect(keys).not.toContain(`/app-assets/by-id/${app.id}/child.deadbeef.js`)
      expect(keys).not.toContain(`/app-embeds/by-id/${app.id}/child.deadbeef.js`)

      const proveHostileSvgOpaque = async (hostileContext, { seedOwner }) => {
        const hostile = await hostileContext.newPage()
        if (seedOwner) {
          // offline.html is inert and does not register the shell worker, so a
          // fresh context exercises the direct-network response first.
          await hostile.goto(`${BASE}/offline.html`, { waitUntil: 'domcontentloaded' })
          await hostile.evaluate(ownerToken => localStorage.setItem('token', ownerToken), token)
        }
        const hostileUrl = HOSTILE_ORIGIN
          ? new URL(HOSTILE_ORIGIN)
          : new URL(BASE)
        if (!HOSTILE_ORIGIN) {
          hostileUrl.hostname = hostileUrl.hostname === '127.0.0.1' ? 'localhost' : '127.0.0.1'
        }
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
        await hostile.close()
      }

      const directContext = await browser.newContext({ ignoreHTTPSErrors: true })
      try {
        await proveHostileSvgOpaque(directContext, { seedOwner: true })
      } finally {
        await directContext.close()
      }
      // Repeat after the shell worker controls BASE. This is the regression
      // against cached responses accidentally restoring shell-origin authority.
      await proveHostileSvgOpaque(context, { seedOwner: false })

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
      await expect.poll(async () => {
        const offlineChild = page.frames().find(
          frame => frame.url().includes('/app-embeds/by-id/'),
        )
        return offlineChild ? offlineChild.title() : ''
      }).toBe('Opaque packaged fixture')
      child = page.frames().find(frame => frame.url().includes('/app-embeds/by-id/'))
      expect(child).toBeTruthy()
      await expect(child.locator('#packaged')).toHaveText('real packaged document')
    } finally {
      await context.setOffline(false)
      await request.delete(`${BASE}/api/apps/${app.id}`, {
        headers, failOnStatusCode: false,
      })
    }
  })
})
