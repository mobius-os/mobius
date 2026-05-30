// Tier 2 — chat offline affordance. Chat is online-only; when the
// browser goes offline the composer must disable Send and say so, and
// re-enable when the connection returns. Needs a live mobius-test
// container (MOBIUS_URL / default :8001) + the auth state from setup.
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test('composer disables + notes when offline, re-enables online', async ({ page, context }) => {
  await page.goto(`${BASE}/shell/`)
  // Type into the composer so the primary action resolves to Send.
  const input = page.getByPlaceholder('Message Möbius…')
  await input.waitFor()
  await input.fill('hello')
  await expect(page.locator('button[aria-label="Send"]')).toBeEnabled()

  await context.setOffline(true)
  await expect(page.getByText("You're offline — chat needs a connection.")).toBeVisible()
  await expect(page.locator('button[aria-label="Send"]')).toBeDisabled()

  await context.setOffline(false)
  await expect(page.locator('button[aria-label="Send"]')).toBeEnabled()
})
