import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'
import { mockAcceptedMessages } from './_mockAcceptedMessages.mjs'

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
  const paintedChat = page.locator('[data-chat-surface="painted"]')
  const composer = paintedChat.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(composer).toBeVisible({ timeout: 8000 })
  await composer.fill('Keep this file with my unfinished message')
  await paintedChat.locator('input[type="file"]').setInputFiles({
    name: 'draft-note.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('draft attachment'),
  })
  await expect(paintedChat.getByRole('button', { name: 'Remove draft-note.txt' }))
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
  await expect(page.locator('[data-chat-surface="painted"]')
    .getByRole('textbox', { name: 'Message Möbius…' }))
    .toBeVisible({ timeout: 8000 })
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(draftChat.id)}`, {
    waitUntil: 'domcontentloaded',
  })

  await expect(composer).toHaveValue('Keep this file with my unfinished message')
  await expect(paintedChat.getByRole('button', { name: 'Remove draft-note.txt' })).toBeVisible()

  let sentBody = null
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
    sentBody = JSON.parse(route.request().postData() || '{}')
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'started' }),
    })
  })
  await composer.press('Enter')
  await expect.poll(() => sentBody?.attachments?.map(file => file.name) || [])
    .toEqual(['draft-note.txt'])
})

test('a sent image keeps one geometry while its media token resolves', async ({ page }) => {
  await page.setViewportSize({ width: 1512, height: 861 })
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' }))
  await mockAcceptedMessages(page)

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'attachment-send-geometry')
  expect(chat?.id).toBeTruthy()

  let releaseMediaToken
  let mediaTokenRequested
  const requested = new Promise(resolve => { mediaTokenRequested = resolve })
  const released = new Promise(resolve => { releaseMediaToken = resolve })
  const imageSvg = [
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">',
    '<rect width="640" height="480" fill="#5f7f78"/>',
    '</svg>',
  ].join('')
  await page.route(new RegExp(`/api/chats/${chat.id}/media-token$`), async route => {
    mediaTokenRequested()
    await released
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ token: 'attachment-geometry-test' }),
    })
  })
  await page.route(
    new RegExp(`/api/chats/${chat.id}/uploads/send-geometry\\.svg(?:\\?.*)?$`),
    route => route.fulfill({
      status: 200,
      contentType: 'image/svg+xml',
      body: imageSvg,
    }),
  )

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const paintedChat = page.locator('[data-chat-surface="painted"]')
  const composer = paintedChat.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(composer).toBeVisible({ timeout: 8000 })
  await paintedChat.locator('input[type="file"]').setInputFiles({
    name: 'send-geometry.svg',
    mimeType: 'image/svg+xml',
    buffer: Buffer.from(imageSvg),
  })
  await expect(paintedChat.getByRole('button', { name: 'Remove send-geometry.svg' }))
    .toBeVisible({ timeout: 8000 })
  const composerCard = await paintedChat.locator('.chat__attach-card--image')
    .evaluate(element => {
      const rect = element.getBoundingClientRect()
      return { width: rect.width, height: rect.height }
    })

  await composer.fill('Image attachment geometry check')
  const send = paintedChat.getByRole('button', { name: 'Send', exact: true })
  await expect(send).toBeEnabled()
  await send.click()
  await requested

  const userRow = paintedChat.locator('.chat__msg--user').last()
  const frame = userRow.locator('.chat__attach-thumb-frame')
  await expect(frame).toBeVisible()
  await expect(frame.locator('img')).toHaveCount(0)

  const before = await userRow.evaluate(row => {
    const scroll = row.closest('.chat__scroll')
    const frameEl = row.querySelector('.chat__attach-thumb-frame')
    const rowRect = row.getBoundingClientRect()
    const scrollRect = scroll.getBoundingClientRect()
    const frameRect = frameEl.getBoundingClientRect()
    return {
      rowTop: rowRect.top - scrollRect.top,
      rowHeight: rowRect.height,
      scrollTop: scroll.scrollTop,
      frameWidth: frameRect.width,
      frameHeight: frameRect.height,
    }
  })
  // Compare painted geometry rather than literal CSS pixels: desktop shell
  // density intentionally scales the whole chat while mobile stays at 100%.
  expect(Math.abs(before.frameWidth - composerCard.width)).toBeLessThanOrEqual(1)
  expect(Math.abs(before.frameHeight - composerCard.height)).toBeLessThanOrEqual(1)

  releaseMediaToken()
  await expect(frame.locator('img')).toBeVisible()
  await page.evaluate(() => new Promise(resolve => (
    requestAnimationFrame(() => requestAnimationFrame(resolve))
  )))

  const after = await userRow.evaluate(row => {
    const scroll = row.closest('.chat__scroll')
    const frameEl = row.querySelector('.chat__attach-thumb-frame')
    const rowRect = row.getBoundingClientRect()
    const scrollRect = scroll.getBoundingClientRect()
    const frameRect = frameEl.getBoundingClientRect()
    return {
      rowTop: rowRect.top - scrollRect.top,
      rowHeight: rowRect.height,
      scrollTop: scroll.scrollTop,
      frameWidth: frameRect.width,
      frameHeight: frameRect.height,
    }
  })
  expect(after.frameWidth).toBe(before.frameWidth)
  expect(after.frameHeight).toBe(before.frameHeight)
  expect(Math.abs(after.rowHeight - before.rowHeight)).toBeLessThanOrEqual(1)
  expect(Math.abs(after.rowTop - before.rowTop)).toBeLessThanOrEqual(1)
  expect(Math.abs(after.scrollTop - before.scrollTop)).toBeLessThanOrEqual(1)
})
