import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CHAT_ID = '70000000-0000-4000-8000-000000000007'
const IMAGES = [
  ['Alpine lake', '#6d8f96'],
  ['Forest path', '#6e8065'],
  ['Ocean cliff', '#857a70'],
  ['Night market', '#6b647f'],
]

test.use({ serviceWorkers: 'block' })

function imageMarkup() {
  return IMAGES
    .map(([name], index) => `![${name}](/api/gallery-fixture/${index + 1}.svg)`)
    .join('\n\n')
}

function chatListItem() {
  return {
    id: CHAT_ID,
    title: 'Image gallery fixture',
    created_at: '2026-07-24T00:00:00Z',
    updated_at: '2026-07-24T00:00:00Z',
    activity_at: '2026-07-24T00:00:00Z',
    pinned_at: null,
    created_by_app_id: null,
    has_messages: true,
    running: false,
    run_status: null,
  }
}

async function setupGallery(page, viewport) {
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
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        messages: [
          { role: 'user', content: 'Show the related photos', ts: 1700000000000, blocks: [] },
          { role: 'assistant', content: imageMarkup(), ts: 1700000000001, blocks: [] },
        ],
        total: 2,
        offset: 0,
        running: false,
        pending_messages: [],
      }),
    })
  })
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route(/\/api\/gallery-fixture\/\d+\.svg(?:\?.*)?$/, route => {
    const filename = new URL(route.request().url()).pathname.split('/').pop()
    const index = Number(filename.replace('.svg', '')) - 1
    const [name, color] = IMAGES[index]
    return route.fulfill({
      status: 200,
      contentType: 'image/svg+xml',
      body: [
        '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600">',
        `<rect width="800" height="600" fill="${color}"/>`,
        `<text x="48" y="540" fill="white" font-size="52">${name}</text>`,
        '</svg>',
      ].join(''),
    })
  })

  await page.goto(BASE, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.locator('.md-image-gallery')).toBeVisible({ timeout: 10_000 })
  await expect(page.locator('.md-image-gallery__item')).toHaveCount(IMAGES.length)
  await expect(page.locator('.md-image-gallery img')).toHaveCount(IMAGES.length)
}

async function dispatchSwipe(page, selector, fromX, toX) {
  const target = page.locator(selector)
  const box = await target.boundingBox()
  if (!box) throw new Error(`No box for ${selector}`)
  const client = await page.context().newCDPSession(page)
  const y = box.y + box.height / 2
  const startX = box.x + box.width * fromX
  const endX = box.x + box.width * toX

  await client.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: startX, y, radiusX: 4, radiusY: 4 }],
  })
  for (let step = 1; step <= 5; step += 1) {
    const x = startX + ((endX - startX) * step) / 5
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x, y, radiusX: 4, radiusY: 4 }],
    })
  }
  await client.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: [],
  })
}

test('desktop filmstrip is compact and supports buttons, keyboard, and viewer navigation', async ({ page }) => {
  await setupGallery(page, { width: 1180, height: 820 })

  const rail = page.locator('.md-image-gallery__rail')
  const geometry = await rail.evaluate(element => {
    const item = element.querySelector('.md-image-gallery__item')
    return {
      railWidth: element.getBoundingClientRect().width,
      itemWidth: item.getBoundingClientRect().width,
      scrollWidth: element.scrollWidth,
    }
  })
  expect(geometry.itemWidth / geometry.railWidth).toBeGreaterThan(0.30)
  expect(geometry.itemWidth / geometry.railWidth).toBeLessThan(0.35)
  expect(geometry.scrollWidth).toBeGreaterThan(geometry.railWidth)

  const next = page.getByRole('button', { name: 'Next images' })
  await expect(next).toBeEnabled()
  await next.click()
  await expect.poll(() => rail.evaluate(element => element.scrollLeft)).toBeGreaterThan(20)

  await rail.focus()
  await page.keyboard.press('ArrowLeft')
  await expect.poll(() => rail.evaluate(element => element.scrollLeft)).toBeLessThan(20)

  await page.getByRole('button', { name: 'Open Forest path preview' }).click()
  await expect(page.getByRole('dialog', { name: /Image 2 of 4/ })).toBeVisible()
  await expect(page.locator('.lightbox-count')).toHaveText('2 / 4')
  await page.keyboard.press('ArrowRight')
  await expect(page.locator('.lightbox-count')).toHaveText('3 / 4')
  await expect(page.locator('.lightbox-image')).toHaveAttribute('alt', 'Ocean cliff')
  await page.keyboard.press('Escape')
  await expect(page.locator('.lightbox-overlay')).toHaveCount(0)
})

test('mobile filmstrip uses native touch scrolling and viewer swipe navigation', async ({ page }) => {
  const client = await page.context().newCDPSession(page)
  await client.send('Emulation.setTouchEmulationEnabled', {
    enabled: true,
    maxTouchPoints: 5,
  })
  await setupGallery(page, { width: 390, height: 844 })

  const rail = page.locator('.md-image-gallery__rail')
  const widthRatio = await rail.evaluate(element => (
    element.querySelector('.md-image-gallery__item').getBoundingClientRect().width
    / element.getBoundingClientRect().width
  ))
  expect(widthRatio).toBeGreaterThan(0.74)
  expect(widthRatio).toBeLessThan(0.82)

  await dispatchSwipe(page, '.md-image-gallery__rail', 0.82, 0.18)
  await expect.poll(() => rail.evaluate(element => element.scrollLeft)).toBeGreaterThan(30)

  await page.getByRole('button', { name: 'Open Forest path preview' }).click()
  await expect(page.locator('.lightbox-count')).toHaveText('2 / 4')
  await dispatchSwipe(page, '.lightbox-image', 0.82, 0.18)
  await expect(page.locator('.lightbox-count')).toHaveText('3 / 4')
  await expect(page.locator('.lightbox-image')).toHaveAttribute('alt', 'Ocean cliff')
})
