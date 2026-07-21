/**
 * Provider availability is authoritative in the chat model picker.
 * A registry can advertise models for installed CLIs that have no usable
 * credentials; those rows must never become actionable choices.
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

test('picker exposes configured providers without leaking unavailable registry rows', async ({ page }) => {
  await page.route(/\/api\/auth\/providers\/status$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      codex: { configured: true, authenticated: true },
      claude: { configured: false, authenticated: false },
    }),
  }))
  await page.route(/\/api\/models$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      providers: {
        codex: [
          { id: 'codex-fast', label: 'Codex Fast', available: true },
          { id: 'codex-deep', label: 'Codex Deep', available: true },
        ],
        claude: [
          { id: 'claude-one', label: 'Claude One', available: true },
          { id: 'claude-two', label: 'Claude Two', available: true },
          { id: 'claude-three', label: 'Claude Three', available: true },
        ],
      },
    }),
  }))
  await page.route(/\/api\/owner\/model-prefs$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ hidden_ids: [] }),
  }))

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'provider-availability')
  expect(chat?.id).toBeTruthy()
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.locator('.chat__form')).toBeVisible()

  await page.getByRole('button', { name: 'Attach or change model' }).click()

  const configuredRows = page.locator('button.csp-row:not([disabled])')
    .filter({ hasText: 'OpenAI Codex' })
  await expect(configuredRows).toHaveCount(2)

  // If Claude happens to be the owner's saved provider, its one selected row
  // may remain for context. The other registry rows must stay hidden and the
  // retained row must be disabled and clearly marked unavailable.
  const unavailableRows = page.locator('button.csp-row').filter({ hasText: 'Claude Code' })
  const unavailableCount = await unavailableRows.count()
  expect(unavailableCount).toBeLessThanOrEqual(1)
  if (unavailableCount === 1) {
    await expect(unavailableRows).toBeDisabled()
    await expect(unavailableRows).toContainText('Not connected')
  }
})
