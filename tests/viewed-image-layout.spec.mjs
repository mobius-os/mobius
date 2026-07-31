/** Cold viewed-image loads must reveal once, after decode fixes final layout. */
import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CHAT_ID = '70000000-0000-4000-8000-000000000008'
const IMAGE = readFileSync(new URL('./fixtures/viewed-image-800x600.png', import.meta.url))

test.use({ serviceWorkers: 'block' })

function chatListItem() {
  return {
    id: CHAT_ID,
    title: 'Viewed image layout fixture',
    created_at: '2026-07-29T00:00:00Z',
    updated_at: '2026-07-29T00:00:00Z',
    activity_at: '2026-07-29T00:00:00Z',
    pinned_at: null,
    created_by_app_id: null,
    has_messages: true,
    running: false,
  }
}

async function verifyColdImageLayout(page, viewport) {
  await page.setViewportSize(viewport)
  await page.addInitScript(chatId => {
    localStorage.setItem('moebius_active_chat', chatId)
  }, CHAT_ID)

  await page.route(/\/api\/chats(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([chatListItem()]),
    })
  })
  await page.route(new RegExp(`/api/chats/${CHAT_ID}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const messages = [
      { role: 'user', content: 'Please inspect the image.', ts: 1700000000000, blocks: [] },
      {
        role: 'assistant',
        content: '',
        ts: 1700000000001,
        blocks: [{
          type: 'tool',
          tool: 'ViewImage',
          input: `/data/chats/${CHAT_ID}/media/cold.png`,
          output: '',
          status: 'done',
          tool_use_id: 'cold-viewed-image',
        }],
      },
    ]
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        messages,
        total: messages.length,
        offset: 0,
        running: false,
        pending_messages: [],
      }),
    })
  })
  await page.route(new RegExp(`/api/chats/${CHAT_ID}/stream$`), route =>
    route.fulfill({ status: 204, body: '' }))
  await page.route(new RegExp(`/api/chats/${CHAT_ID}/media-token$`), route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ token: 'cold-image-token' }),
    }))

  let markImageRequested
  let releaseImage
  const imageRequested = new Promise(resolve => { markImageRequested = resolve })
  const imageGate = new Promise(resolve => { releaseImage = resolve })
  await page.route(new RegExp(`/api/chats/${CHAT_ID}/media/cold\\.png(?:\\?.*)?$`), async route => {
    markImageRequested()
    await imageGate
    return route.fulfill({
      status: 200,
      contentType: 'image/png',
      body: IMAGE,
    })
  })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(CHAT_ID)}`, {
    waitUntil: 'domcontentloaded',
  })
  const painted = page.locator('[data-chat-surface="painted"]')
  const toggle = painted.getByRole('button', { name: /Viewed .*cold\.png/ })
  await expect(toggle).toBeVisible({ timeout: 10_000 })
  const detail = painted.locator('.chat__tool--image .chat__tool-detail')
  await detail.evaluate(element => {
    window.__viewedImageHeights = []
    window.__viewedImageObserver = new ResizeObserver(entries => {
      window.__viewedImageHeights.push(Math.round(entries[0].contentRect.height))
    })
    window.__viewedImageObserver.observe(element)
  })

  const before = await toggle.boundingBox()
  expect(before).not.toBeNull()
  await toggle.click()
  await imageRequested

  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(toggle).toHaveAttribute('aria-busy', 'true')
  await expect(detail).toBeHidden()
  await expect(detail.locator('img')).toHaveCount(0)
  releaseImage()

  await expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await expect(toggle).not.toHaveAttribute('aria-busy', /.+/)
  const preview = painted.getByRole('button', { name: 'Open cold.png preview' })
  await expect(preview).toBeVisible()
  await expect(preview).toBeEnabled()
  await expect(preview.locator('img')).toHaveAttribute('width', '800')
  await expect(preview.locator('img')).toHaveAttribute('height', '600')
  await page.evaluate(() => new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))))

  const after = await toggle.boundingBox()
  expect(after).not.toBeNull()
  expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(0.5)

  const heights = await page.evaluate(() => {
    window.__viewedImageObserver.disconnect()
    return window.__viewedImageHeights
  })
  const firstExpanded = heights.findIndex(height => height > 0)
  expect(firstExpanded).toBeGreaterThanOrEqual(0)
  const expansion = heights.slice(firstExpanded)
  expect(new Set(expansion).size).toBe(1)
}

for (const [surface, viewport] of [
  ['desktop', { width: 1180, height: 820 }],
  ['phone', { width: 390, height: 844 }],
]) {
  test(`a cold viewed image reveals at final height on ${surface}`, async ({ page }) => {
    await verifyColdImageLayout(page, viewport)
  })
}
