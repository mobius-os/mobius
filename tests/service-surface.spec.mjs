/** Dedicated service-origin topology: shell -> adapter -> same-origin service. */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const baseUrl = new URL(BASE)
const SERVICE_ORIGIN = process.env.MOBIUS_TEST_TANDOOR_ORIGIN
  || `http://tandoor.localhost:${baseUrl.port || '80'}`
const FAKE_UPSTREAM = process.env.MOBIUS_FAKE_TANDOOR_UPSTREAM || 'http://127.0.0.1:8123'
const INTERNAL_API = process.env.MOBIUS_TEST_INTERNAL_API
if (!INTERNAL_API) {
  throw new Error(
    'service-surface.spec requires MOBIUS_TEST_INTERNAL_API for independent cleanup',
  )
}

function cspSources(value, directive) {
  const entry = String(value || '').split(';')
    .map(part => part.trim())
    .find(part => part === directive || part.startsWith(`${directive} `))
  return entry ? entry.split(/\s+/).slice(1) : []
}

async function boundedFetch(path, { token, ...options } = {}) {
  return fetch(`${INTERNAL_API}${path}`, {
    ...options,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
    signal: AbortSignal.timeout(5_000),
  })
}

// This contract mutates one fixed service slug, so retrying in the same
// disposable database would test a tombstoned `tandoor-2`, not the topology.
// It is security-sensitive and should pass once, not retry into green.
test.describe.configure({ retries: 0 })
// Use Chromium's native SW behavior here. Playwright's `serviceWorkers:
// 'block'` shim reads navigator.serviceWorker inside every child and injects a
// runner-owned SecurityError into the deliberately opaque app frame.

const TANDOOR_WRAPPER = `
import { useEffect } from 'react'
export default function App(){
  useEffect(()=>{ window.parent.postMessage({type:'moebius:open-service',service:'tandoor'},'*') },[])
  return <main id="tandoor-wrapper">Opening Tandoor…</main>
}`

async function ownerToken(context) {
  const state = await context.storageState()
  const baseOrigin = new URL(BASE).origin
  const origin = state.origins.find(item => item.origin === baseOrigin)
  return origin?.localStorage.find(item => item.name === 'token')?.value || null
}

test('service adapter stays branded until heartbeat, then preserves cookies and re-covers navigation failure', async ({ page, context, request }) => {
  // Read the auth project's persisted token without opening the shell. The app
  // must exist before the page's first navigation so this is a real cold
  // deep-link, not a race against an already-hydrated app list.
  const token = await ownerToken(context)
  expect(token).toBeTruthy()
  const headers = { Authorization: `Bearer ${token}` }
  const previousConfig = await request.get(`${BASE}/api/fs/read?path=local-services.json`, {
    headers, failOnStatusCode: false,
  })
  const previousBody = previousConfig.ok() ? await previousConfig.text() : null
  const config = JSON.stringify({
    version: 1,
    services: { tandoor: { upstream: FAKE_UPSTREAM, public_surface: true } },
  })
  const writeConfig = body => request.put(
    `${BASE}/api/fs/write?path=local-services.json`,
    { headers: { ...headers, 'Content-Type': 'text/plain' }, data: body },
  )
  let app = null
  let configChanged = false

  const consoleErrors = []
  const pageErrors = []
  let blockedAdapterRequests = 0
  const adapterRequests = []
  const blockedAdapterUrls = new Set()
  const uniqueAdapterUrls = () => [...new Set(adapterRequests)]
  const fakePingRequests = []
  const fakePingRequestObjects = []
  const fakePingResponses = []
  const fakePingFailures = []
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('request', request => {
    if (request.url().startsWith(`${SERVICE_ORIGIN}/services/tandoor/_mobius/surface`)) {
      adapterRequests.push(request.url())
    }
    if (request.url() === `${SERVICE_ORIGIN}/services/tandoor/api/ping`) {
      fakePingRequests.push(request.url())
      fakePingRequestObjects.push(request)
    }
  })
  page.on('response', response => {
    if (response.url() === `${SERVICE_ORIGIN}/services/tandoor/api/ping`) {
      fakePingResponses.push(response.status())
    }
  })
  page.on('requestfailed', request => {
    if (request.url() === `${SERVICE_ORIGIN}/services/tandoor/api/ping`) {
      fakePingFailures.push(request.failure()?.errorText || 'unknown failure')
    }
  })
  await page.addInitScript(() => {
    window.__mobiusServiceReadyEvents = []
    window.addEventListener('message', event => {
      if (event.data?.type !== 'moebius:service-ready') return
      const frame = [...document.querySelectorAll('iframe[src*="/_mobius/surface"]')]
        .find(candidate => candidate.contentWindow === event.source)
      window.__mobiusServiceReadyEvents.push({
        origin: event.origin,
        sourceMatchesSurfaceFrame: Boolean(frame),
        frameSrc: frame?.src || null,
        message: event.data,
      })
    })
  })

  const adapterPattern = `${SERVICE_ORIGIN}/services/tandoor/_mobius/surface*`
  await page.route(adapterPattern, async route => {
    blockedAdapterRequests += 1
    blockedAdapterUrls.add(route.request().url())
    const response = await route.fetch()
    await route.fulfill({
      response,
      headers: {
        ...response.headers(),
        'x-frame-options': 'DENY',
        'content-security-policy': "frame-ancestors 'none'",
      },
    })
  })

  try {
    const configWrite = await writeConfig(config)
    expect(configWrite.ok()).toBeTruthy()
    configChanged = true
    const surfaceResponse = await request.get(
      `${BASE}/api/local-services/tandoor/surface`,
      { headers, failOnStatusCode: false },
    )
    expect(surfaceResponse.status()).toBe(200)
    expect(await surfaceResponse.json()).toEqual({
      slug: 'tandoor',
      url: `${SERVICE_ORIGIN}/services/tandoor/_mobius/surface`,
    })

    const created = await request.post(`${BASE}/api/apps/`, {
      headers,
      data: {
        name: 'Tandoor', description: 'Disposable dedicated service fixture.',
        jsx_source: TANDOOR_WRAPPER,
      },
    })
    expect(created.status()).toBe(201)
    app = await created.json()
    expect(app.slug).toBe('tandoor')

    const shellResponse = await page.goto(
      `${BASE}/shell/?app=${app.id}`,
      { waitUntil: 'domcontentloaded' },
    )
    // The shell may frame only itself and the one configured service origin.
    // Pin this from the actual proxy response so a Caddy regression cannot be
    // mistaken for a route/interception failure again.
    expect(cspSources(shellResponse.headers()['content-security-policy'], 'frame-src'))
      .toEqual(["'self'", SERVICE_ORIGIN])
    const appFrame = page.locator(`iframe[src*="/api/apps/${app.id}/frame"]`)
    await expect(appFrame).toHaveCount(1, { timeout: 5_000 })
    const appSandbox = (await appFrame.getAttribute('sandbox') || '').split(/\s+/)
    expect(appSandbox).not.toContain('allow-same-origin')
    // load fires even for the deliberately blocked document. While the shell
    // waits for the absent heartbeat, the frame must remain fully covered.
    await expect.poll(() => blockedAdapterRequests, { timeout: 5_000 }).toBeGreaterThan(0)
    await expect.poll(() => uniqueAdapterUrls().length, { timeout: 5_000 }).toBeGreaterThan(0)
    await expect.poll(
      () => consoleErrors.some(text => /frame|ancestor|x-frame-options/i.test(text)),
      { timeout: 5_000 },
    ).toBe(true)
    const blockedFrame = page.locator('iframe[src*="/_mobius/surface"]')
    await expect(blockedFrame).toHaveCSS('opacity', '0')
    // No heartbeat arrives, so the shell transitions to its own error and
    // removes the failed browser document rather than ever uncovering it.
    await expect(page.getByText(`Couldn’t open Tandoor`)).toBeVisible({ timeout: 30_000 })
    expect(blockedAdapterRequests).toBeGreaterThan(0)
    expect(consoleErrors.some(text => /frame|ancestor|x-frame-options/i.test(text))).toBe(true)
    await expect(blockedFrame).toHaveCount(0)
    // A first-load service-worker handoff may reload the shell and therefore
    // create a fresh correlated surface before the user acts. Treat every URL
    // observed during that stabilization window as the baseline; the contract
    // below is one new correlation per explicit Open/Retry, not one request for
    // the entire browser lifetime.
    const baselineAdapterUrls = uniqueAdapterUrls()
    const baselineBlockedCount = blockedAdapterUrls.size
    expect(baselineAdapterUrls.length).toBeGreaterThan(0)
    expect(baselineBlockedCount).toBe(baselineAdapterUrls.length)

    await page.getByRole('button', { name: 'Close' }).click({ timeout: 5_000 })
    await expect(page.getByText('Tandoor is closed')).toBeVisible({ timeout: 5_000 })
    await expect(page.locator('iframe[src*="/_mobius/surface"]')).toHaveCount(0)
    await page.waitForTimeout(500)
    expect(uniqueAdapterUrls()).toEqual(baselineAdapterUrls)

    await page.getByRole('button', { name: 'Open Tandoor' }).click({ timeout: 5_000 })
    await expect.poll(() => uniqueAdapterUrls().length, { timeout: 5_000 })
      .toBe(baselineAdapterUrls.length + 1)
    await expect.poll(() => blockedAdapterUrls.size, { timeout: 5_000 })
      .toBe(baselineBlockedCount + 1)
    const secondBlockedUrl = uniqueAdapterUrls().at(-1)
    expect(baselineAdapterUrls).not.toContain(secondBlockedUrl)
    await expect(page.locator('iframe[src*="/_mobius/surface"]')).toHaveCSS('opacity', '0')
    await expect(page.getByText(`Couldn’t open Tandoor`)).toBeVisible({ timeout: 30_000 })

    const blockedUrlCount = uniqueAdapterUrls().length
    const blockedRouteCount = blockedAdapterUrls.size
    await page.unroute(adapterPattern)
    consoleErrors.length = 0
    await page.getByRole('button', { name: 'Retry' }).click({ timeout: 5_000 })
    await expect.poll(() => uniqueAdapterUrls().length, { timeout: 5_000 })
      .toBe(blockedUrlCount + 1)
    expect(blockedAdapterUrls.size).toBe(blockedRouteCount) // proves the blocking route is gone
    const secondAdapterUrl = uniqueAdapterUrls().at(-1)
    expect(secondAdapterUrl).not.toBe(secondBlockedUrl)

    const serviceFrame = page.locator('iframe[src*="/_mobius/surface"]')
    await expect(serviceFrame).toHaveCount(1, { timeout: 5_000 })
    const secondFrameSrc = await serviceFrame.getAttribute('src')
    const secondCorrelation = decodeURIComponent(new URL(secondFrameSrc).hash.slice(1))

    await expect.poll(() => page.frames().some(frame => (
      frame.url().startsWith(`${SERVICE_ORIGIN}/services/tandoor/`)
      && !frame.url().includes('/_mobius/surface')
    )), { timeout: 8_000 }).toBe(true)
    const adapter = page.frames().find(frame => frame.url().includes('/_mobius/surface'))
    const service = page.frames().find(frame => (
      frame.url().startsWith(`${SERVICE_ORIGIN}/services/tandoor/`)
      && !frame.url().includes('/_mobius/surface')
    ))
    expect(adapter).toBeTruthy()
    expect(service).toBeTruthy()
    expect(adapter.url()).toBe(secondFrameSrc)
    await expect.poll(() => fakePingRequests.length, { timeout: 5_000 }).toBe(1)
    await expect.poll(() => fakePingResponses, { timeout: 5_000 }).toEqual([200])
    expect(fakePingFailures).toEqual([])
    const pingHeaders = await fakePingRequestObjects[0].allHeaders()
    expect(pingHeaders.cookie || '').toContain('fake_tandoor=session')
    await expect(service.locator('#status')).toHaveText('ready', { timeout: 5_000 })
    await expect.poll(
      () => page.evaluate(() => window.__mobiusServiceReadyEvents.length),
      { timeout: 5_000 },
    ).toBe(1)
    const [readyEvent] = await page.evaluate(() => window.__mobiusServiceReadyEvents)
    expect(readyEvent.origin).toBe(SERVICE_ORIGIN)
    expect(readyEvent.sourceMatchesSurfaceFrame).toBe(true)
    expect(readyEvent.frameSrc).toBe(secondFrameSrc)
    expect(readyEvent.message).toEqual({
      type: 'moebius:service-ready', service: 'tandoor', correlation: secondCorrelation,
    })
    await expect(serviceFrame).toHaveCSS('opacity', '1', { timeout: 5_000 })
    expect(await service.evaluate(() => localStorage.getItem('fake-tandoor-storage'))).toBe('works')
    expect(await service.evaluate(() => localStorage.getItem('token'))).toBeNull()
    const cookieUrl = `${SERVICE_ORIGIN}/services/tandoor/`
    const cookies = await page.context().cookies(cookieUrl)
    const cookie = cookies.find(item => item.name === 'fake_tandoor')
    expect(cookie?.domain).toBe(new URL(SERVICE_ORIGIN).hostname)
    expect(cookie?.domain.startsWith('.')).toBe(false)
    expect(cookie?.path).toBe('/services/tandoor')
    expect(cookie?.sameSite).toBe('None')
    expect(cookie?.secure).toBe(true)
    const childHost = new URL(SERVICE_ORIGIN)
    childHost.hostname = `child.${childHost.hostname}`
    childHost.pathname = '/services/tandoor/'
    const cookieProbe = await page.context().newPage()
    const childRequestPromise = cookieProbe.waitForRequest(childHost.href)
    await cookieProbe.goto(childHost.href, { waitUntil: 'domcontentloaded' })
    const childRequest = await childRequestPromise
    const childHeaders = await childRequest.allHeaders()
    expect(childHeaders.cookie || '').not.toContain('fake_tandoor=')
    await cookieProbe.close()
    const shellProbe = await page.context().newPage()
    const shellHealthUrl = `${BASE}/api/health`
    const shellRequestPromise = shellProbe.waitForRequest(shellHealthUrl)
    await shellProbe.goto(shellHealthUrl, { waitUntil: 'domcontentloaded' })
    const shellRequest = await shellRequestPromise
    const shellHeaders = await shellRequest.allHeaders()
    expect(shellHeaders.cookie || '').not.toContain('fake_tandoor=')
    await shellProbe.close()
    expect(consoleErrors.filter(text => /frame-ancestors|x-frame-options/i.test(text))).toEqual([])
    expect(pageErrors).toEqual([])

    // A later navigation failure must re-cover before Chromium's error
    // document can surface. The child becomes unreadable; adapter branding
    // remains the visible document.
    await service.locator('#go-bad').click()
    await expect(adapter.locator('#cover')).toBeVisible()
    await expect(adapter.locator('#app')).toHaveCSS('opacity', '0')
    await expect(page.getByText(/refused to connect|127\.0\.0\.1/i)).toHaveCount(0)
  } finally {
    if (app) {
      const deleted = await boundedFetch(`/api/apps/${app.id}`, {
        token, method: 'DELETE',
      })
      expect(deleted.ok).toBeTruthy()
    }
    if (configChanged) {
      if (previousBody == null) {
        const removed = await boundedFetch('/api/fs/delete?path=local-services.json', {
          token, method: 'DELETE',
        })
        expect(removed.ok).toBeTruthy()
      } else {
        const restored = await boundedFetch('/api/fs/write?path=local-services.json', {
          token,
          method: 'PUT',
          headers: { 'Content-Type': 'text/plain' },
          body: previousBody,
        })
        expect(restored.ok).toBeTruthy()
      }
    }
  }
})
