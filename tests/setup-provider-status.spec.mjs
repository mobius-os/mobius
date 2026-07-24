import { expect, test } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })

test('Codex connects from Settings and stays authoritative when status revalidation fails', async ({ page }) => {
  await page.addInitScript(() => {
    // Older builds persisted this provider-wizard marker. It must never pull a
    // returning owner out of the shell now that agent setup is contextual.
    localStorage.setItem('setup-step', 'provider')
  })

  let authenticationComplete = false
  await page.route(/\/api\/auth\/providers\/status$/, route => (
    authenticationComplete
      ? route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'probe unavailable' }),
        })
      : route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            codex: { authenticated: false },
            claude: { authenticated: false },
          }),
        })
  ))
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
  await page.route(/\/api\/auth\/provider\/codex\/status$/, route => {
    authenticationComplete = true
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'complete' }),
    })
  })
  let settingsWrites = 0
  await page.route(/\/api\/settings$/, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    settingsWrites += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ provider: 'codex' }),
    })
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await expect(page.getByLabel('Toggle navigation')).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('setup-step'))).toBeNull()

  const navigationToggle = page.getByLabel('Toggle navigation')
  if (await navigationToggle.getAttribute('aria-expanded') !== 'true') {
    await navigationToggle.click()
  }
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

  const codexRow = page.locator('.provider-row').filter({ hasText: 'OpenAI Codex' })
  await codexRow.getByRole('button', { name: 'Connect OpenAI Codex' }).click()
  await expect(codexRow.getByText('Allow device access', { exact: true })).toBeVisible()
  await expect(codexRow.getByRole('link', { name: 'Open ChatGPT security' }))
    .toHaveAttribute('href', 'https://chatgpt.com/#settings/Security')
  await expect(codexRow.getByText(/Improve the model for everyone/)).toBeVisible()

  await codexRow.getByRole('button', { name: 'Continue with ChatGPT' }).click()
  await expect(page.getByText('Complete sign-in in your browser.')).toBeVisible()
  await page.evaluate(() => window.dispatchEvent(new Event('pageshow')))

  await expect(codexRow.getByText('Connected', { exact: true })).toBeVisible()
  await expect.poll(() => settingsWrites).toBe(1)
})
