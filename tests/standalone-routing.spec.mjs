/**
 * Standalone app routing regressions.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/standalone-routing.spec.mjs
 */
import { test, expect } from '@playwright/test'

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
  const apps = await list.json()
  const existing = apps.find(app => app.slug === 'cuberun')
  if (existing) {
    const updated = await request.patch(`${BASE}/api/apps/${existing.id}`, {
      headers,
      data: {
        name: 'CubeRun',
        description: 'Standalone routing regression app',
        jsx_source: CUBERUN_SOURCE,
      },
    })
    expect(updated.ok()).toBeTruthy()
    return updated.json()
  }

  const created = await request.post(`${BASE}/api/apps/`, {
    headers,
    data: {
      name: 'CubeRun',
      description: 'Standalone routing regression app',
      jsx_source: CUBERUN_SOURCE,
    },
  })
  expect(created.status()).toBe(201)
  const app = await created.json()
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
  await expect(page.locator('#root')).toContainText('CubeRun standalone smoke')
  expect(await page.title()).toBe('CubeRun')
})
