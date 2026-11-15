import { expectComposerValue, setComposerSelection, composerSelection } from './_composer.mjs'
/* Native word correction preserves selection and focus without browser spelling popups. */
import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// isMobile makes Chromium report a coarse, non-hovering primary pointer.
test.use({ serviceWorkers: 'block', hasTouch: true, isMobile: true })
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
  await composer.evaluate(editor => editor.blur())
  await setComposerSelection(composer, 7, 15)
  await composer.evaluate(editor => editor.focus({ preventScroll: true }))
  const selected = await composerSelection(composer)
  expect(selected).toEqual({ start: 7, end: 15 })
  await page.keyboard.insertText('lessons')
  await expectComposerValue(composer, 'Review lessons carefully')
  await expect(composer).toBeFocused()
})
