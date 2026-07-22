import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })
attachCleanup()

test('an uploaded attachment survives a chat switch and remains sendable', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' }))

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const draftChat = await createTaggedChat(page, 'attachment-draft')
  const otherChat = await createTaggedChat(page, 'attachment-draft-other')

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(draftChat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const composer = page.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(composer).toBeVisible({ timeout: 8000 })
  await composer.fill('Keep this file with my unfinished message')
  await page.locator('input[type="file"]').setInputFiles({
    name: 'draft-note.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('draft attachment'),
  })
  await expect(page.getByRole('button', { name: 'Remove draft-note.txt' }))
    .toBeVisible({ timeout: 8000 })
  await expect.poll(() => page.evaluate((chatId) => {
    const raw = sessionStorage.getItem(`draft:${chatId}`)
    if (!raw) return null
    try {
      const value = JSON.parse(raw)
      return value.attachments?.map(file => file.name) || []
    } catch {
      return []
    }
  }, draftChat.id)).toEqual(['draft-note.txt'])

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(otherChat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.getByRole('textbox', { name: 'Message Möbius…' }))
    .toBeVisible({ timeout: 8000 })
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(draftChat.id)}`, {
    waitUntil: 'domcontentloaded',
  })

  await expect(composer).toHaveValue('Keep this file with my unfinished message')
  await expect(page.getByRole('button', { name: 'Remove draft-note.txt' })).toBeVisible()

  let sentBody = null
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
    sentBody = JSON.parse(route.request().postData() || '{}')
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'started' }),
    })
  })
  await page.keyboard.press('Enter')
  await expect.poll(() => sentBody?.attachments?.map(file => file.name) || [])
    .toEqual(['draft-note.txt'])
})
