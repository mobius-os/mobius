/** Rendered composer contracts for the slash-command picker. */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()
test.use({ serviceWorkers: 'block' })

test('the slash menu follows textarea focus without losing the draft', async ({ page }) => {
  await page.setViewportSize({ width: 426, height: 860 })
  // createTaggedChat reads the already-authenticated app origin; about:blank
  // deliberately denies that localStorage access.
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'slash-focus')
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })

  const paintedChat = page.locator('[data-chat-surface="painted"]')
  const input = paintedChat.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(input).toBeVisible()
  await input.fill('/')
  await expect(page.getByRole('listbox', { name: 'Commands' })).toBeVisible()

  // Tapping the conversation is an intent to leave command picking. The draft
  // stays intact, and focusing the composer again may reopen the same matches.
  await paintedChat.locator('.chat__empty-wrap').click({ position: { x: 8, y: 8 } })
  await expect(page.getByRole('listbox', { name: 'Commands' })).toBeHidden()
  await expect(input).toHaveValue('/')

  await input.focus()
  await expect(page.getByRole('listbox', { name: 'Commands' })).toBeVisible()

  // The footer deliberately lets empty-space taps pass through to the
  // transcript. Its visible command surface must opt back into hit testing so
  // an ordinary pointer click can complete the command without submitting it.
  const reopenedInput = paintedChat.locator('textarea[aria-label="Message Möbius…"]')
  await reopenedInput.fill('/go')
  await page.getByRole('option', { name: /\/goal/ }).click()
  await expect(reopenedInput).toHaveValue('/goal ')
  await expect(reopenedInput).toBeFocused()
  await expect(page.getByRole('listbox', { name: 'Commands' })).toBeHidden()
  await expect(paintedChat.locator('.message--user')).toHaveCount(0)
})
