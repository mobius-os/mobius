import { expect, test } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })

test('successful Codex setup stays authoritative when status revalidation fails', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('setup-step', 'provider')
  })

  await page.route(/\/api\/auth\/providers\/status$/, route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'probe unavailable' }),
  }))
  await page.route(/\/api\/auth\/provider\/codex\/login$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      url: `${BASE}/codex-test-login`,
      code: 'TEST-CODE',
    }),
  }))
  await page.route(/\/codex-test-login$/, route => route.fulfill({
    status: 200,
    contentType: 'text/html',
    body: '<title>Codex test login</title>',
  }))
  await page.route(/\/api\/auth\/provider\/codex\/status$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ status: 'complete' }),
  }))
  await page.route(/\/api\/settings$/, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ provider: 'codex' }),
    })
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('heading', { name: 'Wake up your AI' })).toBeVisible()
  await expect(page.getByRole('alert')).toContainText('Could not verify provider status')

  await page.getByRole('button', { name: 'Connect to Codex' }).click()
  await expect(page.getByText('Complete sign-in in your browser.')).toBeVisible()
  await page.evaluate(() => window.dispatchEvent(new Event('pageshow')))

  await expect(page.getByRole('status')).toContainText('Provider connected')
  await expect(page.getByRole('button', { name: 'Enter Möbius' })).toBeEnabled()
})
