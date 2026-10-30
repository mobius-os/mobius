/* Native word correction preserves selection and focus without browser spelling popups. */
import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block', hasTouch: true })
attachCleanup()

test('returning to a selected word permits correction without moving to the draft end', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'word-correction')
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const composer = page.locator('[data-chat-surface="painted"]')
    .getByRole('textbox', { name: 'Message Möbius…' })
  await expect(composer).toBeVisible()
  await expect(composer).toHaveAttribute('spellcheck', 'false')
  await composer.fill('Review lesssons carefully')

  // Exercise native focus directly: a later synthetic click can mask an
  // onFocus selection rewrite by choosing a different position afterward.
  const selected = await composer.evaluate(textarea => {
    textarea.blur()
    textarea.setSelectionRange(7, 15)
    textarea.focus({ preventScroll: true })
    return { start: textarea.selectionStart, end: textarea.selectionEnd }
  })
  expect(selected).toEqual({ start: 7, end: 15 })
  await page.keyboard.insertText('lessons')
  await expect(composer).toHaveValue('Review lessons carefully')
  await expect(composer).toBeFocused()
})
