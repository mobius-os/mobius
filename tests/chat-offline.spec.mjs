// Tier 2 — chat offline affordance. Chat is online-only; when the browser goes
// offline the shell owns the one persistent warning while the composer disables
// Send, then both recover when the connection returns. Needs a live mobius-test
// container (MOBIUS_URL / default :8001) + the auth state from setup.
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test('shell notes offline once while composer disables, then both recover', async ({ page, context }) => {
  await page.goto(`${BASE}/shell/`)
  // Type into the composer so the primary action resolves to Send.
  const input = page.getByPlaceholder('Message Möbius…')
  await input.waitFor()
  await input.fill('hello')
  await expect(page.locator('[data-chat-surface="painted"] button[aria-label="Send"]')).toBeEnabled()

  await context.setOffline(true)
  await expect(page.locator('.shell__connection-status')).toHaveText(/Offline/i)
  await expect(
    page.locator('[data-chat-surface="painted"]')
      .getByText("You're offline — chat needs a connection."),
  ).toHaveCount(0)
  await expect(page.locator('[data-chat-surface="painted"] button[aria-label="Send"]')).toBeDisabled()

  await context.setOffline(false)
  await expect(page.locator('.shell__connection-status')).toHaveCount(0)
  await expect(page.locator('[data-chat-surface="painted"] button[aria-label="Send"]')).toBeEnabled()
})
