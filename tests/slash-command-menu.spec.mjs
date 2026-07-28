/** Rendered composer contracts for the slash-command picker. */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

test('the slash menu follows textarea focus without losing the draft', async ({ page }) => {
  await page.setViewportSize({ width: 426, height: 860 })
  const chat = await createTaggedChat(page, 'slash-focus')
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })

  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(input).toBeVisible()
  await input.fill('/')
  await expect(page.getByRole('listbox', { name: 'Commands' })).toBeVisible()

  // Tapping the conversation is an intent to leave command picking. The draft
  // stays intact, and focusing the composer again may reopen the same matches.
  await page.locator('.chat__empty-wrap').click({ position: { x: 8, y: 8 } })
  await expect(page.getByRole('listbox', { name: 'Commands' })).toBeHidden()
  await expect(input).toHaveValue('/')

  await input.focus()
  await expect(page.getByRole('listbox', { name: 'Commands' })).toBeVisible()
})
