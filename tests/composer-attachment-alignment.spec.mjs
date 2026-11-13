import { expectComposerValue } from './_composer.mjs'
/** Attachments grow above the composer without moving its text or action. */
import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })
attachCleanup()

for (const viewport of [{ width: 1512, height: 911 }, { width: 390, height: 844 }]) {
  for (const [state, draft] of [
    ['empty', ''],
    ['single-line', 'Keep this text still.'],
    ['multi-line', 'First line\nSecond line\nThird line'],
  ]) {
    test(`${state} text and action stay put when an image is added and removed at ${viewport.width}px`, async ({ page }) => {
      await page.setViewportSize(viewport)
      await page.goto(BASE, { waitUntil: 'domcontentloaded' })
      const chat = await createTaggedChat(page, 'attachment-alignment')
      await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
        waitUntil: 'domcontentloaded',
      })
      const painted = page.locator('[data-chat-surface="painted"]')
      const composer = painted.getByRole('textbox', { name: 'Message Möbius…' })
      await expect(composer).toBeVisible()
      await composer.fill(draft)
      await composer.focus()

      const geometry = () => painted.evaluate(surface => {
        const box = selector => {
          const { x, y, width, height } = surface.querySelector(selector).getBoundingClientRect()
          return { x, y, width, height }
        }
        return { text: box('.chat__input'), action: box('.chat__action') }
      })
      let before
      await expect.poll(async () => {
        const current = await geometry()
        const stable = JSON.stringify(current) === JSON.stringify(before)
        before = current
        return stable
      }).toBe(true)

      await painted.locator('input[type="file"]').setInputFiles({
        name: 'alignment.svg',
        mimeType: 'image/svg+xml',
        buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" fill="#8364f0"/></svg>'),
      })
      const remove = painted.getByRole('button', { name: 'Remove alignment.svg' })
      await expect(remove).toBeVisible()
      await expect(painted.locator('.chat__attach-card-spin')).toHaveCount(0)
      await expect.poll(geometry).toEqual(before)
      await expectComposerValue(composer, draft)
      await expect(composer).toBeFocused()

      await remove.click()
      await expect(remove).toHaveCount(0)
      await expect.poll(geometry).toEqual(before)
      await expectComposerValue(composer, draft)
      await expect(composer).toBeFocused()
    })
  }

  test(`an image stays left as its draft grows and shrinks at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    const chat = await createTaggedChat(page, 'attachment-alignment')
    await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
      waitUntil: 'domcontentloaded',
    })
    const painted = page.locator('[data-chat-surface="painted"]')
    const composer = painted.getByRole('textbox', { name: 'Message Möbius…' })
    await expect(composer).toBeVisible()
    await painted.locator('input[type="file"]').setInputFiles({
      name: 'alignment.svg',
      mimeType: 'image/svg+xml',
      buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" fill="#8364f0"/></svg>'),
    })
    await expect(painted.getByRole('button', { name: 'Remove alignment.svg' })).toBeVisible()
    await expect(painted.locator('.chat__attach-card-spin')).toHaveCount(0)
    const horizontalGeometry = () => painted.evaluate(surface => ({
      cardX: surface.querySelector('.chat__attach-card').getBoundingClientRect().x,
      trayWidth: surface.querySelector('.chat__attach-tray').getBoundingClientRect().width,
      textWidth: surface.querySelector('.chat__input').getBoundingClientRect().width,
    }))
    const before = await horizontalGeometry()
    for (const draft of [
      'Keep this text still.',
      'First line\nSecond line\nThird line',
      Array(30).fill('Long draft line with several words').join('\n'),
      'Back to one line.',
    ]) {
      await composer.fill(draft)
      if (draft.includes('\n')) {
        await expect(painted.locator('.chat__pill')).toHaveAttribute('data-composer-tall', '')
      } else {
        await expect(painted.locator('.chat__pill')).not.toHaveAttribute('data-composer-tall')
      }
      await expect.poll(horizontalGeometry).toEqual(before)
      await expectComposerValue(composer, draft)
      await expect(composer).toBeFocused()
    }
  })
}
