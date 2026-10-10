/**
 * Standalone app routing regressions.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/standalone-routing.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { applyApp } from './app-source.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CUBERUN_SOURCE = `
export default function App() {
  return <main data-testid="cuberun-smoke">CubeRun standalone smoke</main>
}
`

async function ownerToken(page) {
  await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  return token
}

async function ensureCubeRun(request, token) {
  const headers = { Authorization: `Bearer ${token}` }
  const list = await request.get(`${BASE}/api/apps/`, { headers })
  expect(list.ok()).toBeTruthy()
  const result = await applyApp(request, token, {
    slug: 'cuberun',
    name: 'CubeRun',
    description: 'Standalone routing regression app',
    jsxSource: CUBERUN_SOURCE,
  })
  const app = result.app
  expect(app.slug).toBe('cuberun')
  return app
}

test('legacy /cuberun route opens the standalone app, not the Mobius shell', async ({ page, request }) => {
  const token = await ownerToken(page)
  await ensureCubeRun(request, token)

  for (const path of ['/cuberun', '/cuberun/']) {
    const redirect = await request.get(`${BASE}${path}`, { maxRedirects: 0 })
    expect(redirect.status()).toBe(307)
    expect(redirect.headers().location).toBe('/apps/cuberun/')
    expect(redirect.headers()['cache-control']).toBe('no-store')
  }

  const indexHtml = await request.get(`${BASE}/cuberun/index.html`, { maxRedirects: 0 })
  expect(indexHtml.status()).not.toBe(307)
  expect(indexHtml.headers().location).not.toBe('/apps/cuberun/')

  await page.goto(`${BASE}/cuberun`, { waitUntil: 'domcontentloaded' })
  await expect(page).toHaveURL(`${BASE}/apps/cuberun/`)
  await expect(
    page.frameLocator('iframe[data-app-id]').getByTestId('cuberun-smoke'),
  ).toHaveText('CubeRun standalone smoke')
  expect(await page.title()).toBe('CubeRun')
})

const locationSource = revision => `
import React, { useEffect, useState } from 'react'
window.initialLocation = window.mobius.nav.location
window.hasNavLocation = window.mobius.runtimeFeatures.navLocation
window.documentId = Math.random()
export default function App() {
  const [detail, setDetail] = useState(false)
  useEffect(() => {
    window.openDetail = async () => {
      const handle = window.mobius.nav.open('notes', () => {
        setDetail(false)
        window.mobius.nav.setLocation({ detail: null })
      })
      window.ownership = await handle.outcome
      if (window.ownership.status === 'owned') {
        setDetail(true)
        window.mobius.nav.setLocation({ detail: 'notes' })
      }
    }
    if (window.initialLocation?.detail) void window.openDetail()
    else window.mobius.nav.setLocation({ detail: null })
  }, [])
  return <main data-testid="nav-place">{detail ? 'detail: notes' : 'list'} ${revision}</main>
}
`

async function createLocationApp(request, token, revision = 'v1') {
  return (await applyApp(request, token, {
    slug: 'standalone-nav-location', name: 'Standalone location',
    jsxSource: locationSource(revision),
  })).app
}

async function standaloneLocationFrame(page, app) {
  const iframe = page.locator(`iframe.canvas--live[data-app-id="${app.id}"]`)
  await expect(iframe).toBeVisible({ timeout: 15000 })
  const frame = await (await iframe.elementHandle()).contentFrame()
  await frame.waitForFunction(() => Boolean(window.openDetail))
  return frame
}

test.describe('standalone navigation document ownership', () => {
  test.use({ serviceWorkers: 'block' })

  test.beforeEach(async ({ page }) => {
    // Navigation tests do not exercise the first-launch install sheet.
    await page.addInitScript(() => {
      if (window === window.top) {
        sessionStorage.setItem('mobius:install-dismissed:standalone-nav-location', '1')
      }
    })
  })

  test('real wrapper initializes nav.location and repeated host/frame reloads reuse one Back slot', async ({ page, request }) => {
    const token = await ownerToken(page)
    const app = await createLocationApp(request, token)
    await page.goto(`${BASE}/apps/${app.slug}/`, { waitUntil: 'domcontentloaded' })
    let frame = await standaloneLocationFrame(page, app)
    expect(await frame.evaluate(() => window.hasNavLocation)).toBe(true)
    expect(await frame.evaluate(() => window.initialLocation)).toBeNull()
    await frame.evaluate(() => window.openDetail())
    await expect(frame.getByTestId('nav-place')).toHaveText('detail: notes v1')
    const originalDepth = await page.evaluate(() => history.state.mobiusStandaloneDepth)
    expect(originalDepth).toBe(1)
    for (const reloadHost of [false, true, false, true]) {
      const documentId = await frame.evaluate(() => window.documentId)
      if (reloadHost) await page.reload({ waitUntil: 'domcontentloaded' })
      else await frame.goto(frame.url(), { waitUntil: 'load' })
      frame = await standaloneLocationFrame(page, app)
      await expect(frame.getByTestId('nav-place')).toHaveText('detail: notes v1')
      expect(await frame.evaluate(() => window.documentId)).not.toBe(documentId)
      expect(await frame.evaluate(() => window.initialLocation)).toEqual({ detail: 'notes' })
      expect(await page.evaluate(() => history.state.mobiusStandaloneDepth)).toBe(originalDepth)
    }
    await page.evaluate(() => history.back())
    await expect(frame.getByTestId('nav-place')).toHaveText('list v1')
    await expect.poll(() => page.evaluate(() => history.state.mobiusStandaloneDepth)).toBe(0)
  })

  test('a standalone version swap retires the old document and restores one Back slot', async ({ page, request }) => {
    const token = await ownerToken(page)
    const app = await createLocationApp(request, token)
    await page.goto(`${BASE}/apps/${app.slug}/`, { waitUntil: 'domcontentloaded' })
    let frame = await standaloneLocationFrame(page, app)
    await frame.evaluate(() => window.openDetail())
    await expect(frame.getByTestId('nav-place')).toHaveText('detail: notes v1')
    const documentId = await frame.evaluate(() => window.documentId)
    await createLocationApp(request, token, 'v2')
    await page.getByRole('button', { name: /Updated — tap to refresh/ }).click({ timeout: 15000 })
    // Wait for the actual promoted replacement, not the outgoing live frame.
    await expect(page.frameLocator('iframe.canvas--live').getByTestId('nav-place')).toHaveText('detail: notes v2', { timeout: 15000 })
    frame = await standaloneLocationFrame(page, app)
    expect(await frame.evaluate(() => window.documentId)).not.toBe(documentId)
    expect(await frame.evaluate(() => window.initialLocation)).toEqual({ detail: 'notes' })
    expect(await page.evaluate(() => history.state.mobiusStandaloneDepth)).toBe(1)
    await page.evaluate(() => history.back())
    await expect(frame.getByTestId('nav-place')).toHaveText('list v2')
    await expect.poll(() => page.evaluate(() => history.state.mobiusStandaloneDepth)).toBe(0)
  })
})
