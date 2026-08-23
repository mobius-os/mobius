/**
 * Minimal notification-center contract: the shell owns a bell and a bounded
 * recent preview, not a dedicated navigation world. Notification APIs are
 * route-mocked, so this spec never reads or writes backend rows.
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CHAT_ID = '20000000-0000-4000-8000-000000000001'
const CHATS = [{
  id: CHAT_ID,
  title: 'Notify Target Chat',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  activity_at: '2026-01-01T00:00:00Z',
  pinned_at: null,
  created_by_app_id: null,
  has_messages: true,
  running: false,
}]

function notifRows(now = Date.now()) {
  return [
    {
      id: 'n-chat-target', source_type: 'agent', source_id: CHAT_ID,
      title: 'Agent finished your task', body: 'The summary you asked for is ready.',
      icon: null, target: `/shell/?chat=${CHAT_ID}`, actions: null,
      sent_at: new Date(now - 60_000).toISOString(), clicked_at: null, read_at: null,
    },
    {
      id: 'n-hostile-target', source_type: 'app', source_id: '999',
      title: 'Totally legitimate prize', body: 'Click to claim.',
      icon: 'https://evil.example/icon.png', target: 'https://evil.example/phish', actions: null,
      sent_at: new Date(now - 120_000).toISOString(), clicked_at: null, read_at: null,
    },
  ]
}

async function mockNotifications(page, { rows = notifRows(), unreadCount } = {}) {
  const state = {
    rows,
    unreadCount,
    readAllCalls: 0,
    deleteCalls: 0,
    get unread() {
      return this.unreadCount ?? this.rows.filter(row => row.read_at == null).length
    },
  }
  await page.route(/\/api\/notifications\/unread-count$/, route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ count: state.unread }),
  }))
  await page.route(/\/api\/notifications\/read-all$/, route => {
    if (route.request().method() !== 'POST') return route.fallback()
    const updated = state.unread
    const stamp = new Date().toISOString()
    state.rows = state.rows.map(row => (
      row.read_at == null ? { ...row, read_at: stamp } : row
    ))
    state.unreadCount = 0
    state.readAllCalls += 1
    return route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ updated }),
    })
  })
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => {
    if (route.request().method() === 'DELETE') {
      state.deleteCalls += 1
      const deleted = state.rows.length
      state.rows = []
      state.unreadCount = 0
      return route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify({ deleted }),
      })
    }
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(state.rows.slice(0, 8)),
    })
  })
  return state
}

async function setup(page, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)
  await page.addInitScript(chatId => localStorage.setItem('moebius_active_chat', chatId), CHAT_ID)
  await page.route(/\/api\/chats(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CHATS) })
  })
  await page.route(/\/api\/chats\/([0-9a-f-]+)(?:\?.*)?$/, route => {
    if (route.request().method() !== 'GET') return route.fallback()
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ messages: [], total: 0, offset: 0, running: false, pending_messages: [] }),
    })
  })
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => route.fulfill({ status: 204, body: '' }))
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => !!(
    document.querySelector('.chat__empty-wrap')
    || document.querySelector('.chat__scroll')
    || document.querySelector('.chat__form')
  ), { timeout: 10000 })
}

async function openPreview(page) {
  await page.locator('.notification-bell').click()
  await expect(page.locator('.notifications')).toBeVisible()
}

test('bell opens a bounded preview and seen-on-open clears its badge', async ({ page }) => {
  const state = await mockNotifications(page)
  await setup(page)
  const bell = page.locator('.notification-bell')
  await expect(page.locator('.notification-bell__badge')).toHaveText('2')
  await expect(bell).toHaveAccessibleName('Notifications, 2 unread')

  await openPreview(page)
  await expect(page.locator('.notifications__row-title').first()).toHaveText('Agent finished your task')
  await expect.poll(() => state.readAllCalls).toBeGreaterThan(0)
  await expect(page.locator('.notification-bell__badge')).toHaveCount(0)
  await expect(bell).toHaveAttribute('aria-expanded', 'true')

  await expect(page.locator('.notifications__close')).toHaveCount(0)
  await page.locator('.notification-bell').click()
  await expect(page.locator('.notifications')).toBeHidden()
  await expect(bell).toHaveAttribute('aria-expanded', 'false')
})

test('clear all immediately removes the preview rows and badge', async ({ page }) => {
  const state = await mockNotifications(page)
  await setup(page)
  await openPreview(page)

  await expect(page.locator('.notifications__row')).toHaveCount(2)
  await page.getByRole('button', { name: 'Clear all' }).click()
  await expect.poll(() => state.deleteCalls).toBe(1)
  await expect(page.locator('.notifications__row')).toHaveCount(0)
  await expect(page.locator('.notifications__empty')).toBeVisible()
  await expect(page.locator('.notification-bell__badge')).toHaveCount(0)
})

test('bell toggles; Escape and outside click dismiss without navigation', async ({ page }) => {
  await mockNotifications(page)
  await setup(page)
  await openPreview(page)
  await page.locator('.notification-bell').click()
  await expect(page.locator('.notifications')).toBeHidden()

  await openPreview(page)
  await page.keyboard.press('Escape')
  await expect(page.locator('.notifications')).toBeHidden()
  await expect(page.locator('.notification-bell')).toBeFocused()

  await openPreview(page)
  await page.locator('.shell__brand').click({ position: { x: 2, y: 2 } })
  await expect(page.locator('.notifications')).toBeHidden()
})

test('valid targets navigate; hostile targets remain inert', async ({ page }) => {
  await mockNotifications(page)
  await setup(page)
  await openPreview(page)

  const hostile = page.locator('.notifications__row-item', { hasText: 'Totally legitimate prize' })
  await expect(hostile.locator('.notifications__row--link')).toHaveCount(0)
  await expect(page.locator('.notifications img')).toHaveCount(0)

  await page.locator('.notifications__row--link', { hasText: 'Agent finished your task' }).click()
  await expect(page.locator('.notifications')).toBeHidden()
  await page.waitForFunction(
    id => localStorage.getItem('moebius_active_chat') === id,
    CHAT_ID,
    { timeout: 8000 },
  )
})

test('phone header preserves the 44px bell and widest badge without collisions', async ({ page, context }) => {
  await mockNotifications(page, { unreadCount: 120 })
  await setup(page)
  const bell = page.locator('.notification-bell')
  const badge = page.locator('.notification-bell__badge')
  await expect(badge).toHaveText('99+')
  const bellBox = await bell.boundingBox()
  expect(bellBox.width).toBeGreaterThanOrEqual(44)
  expect(bellBox.height).toBeGreaterThanOrEqual(44)

  await context.setOffline(true)
  await page.evaluate(() => window.dispatchEvent(new Event('offline')))
  const pill = page.locator('.shell__connection-status')
  await expect(pill).toBeVisible({ timeout: 15000 })

  const box = locator => locator.boundingBox()
  const [header, brand, pillBox, bellBox2, badgeBox] = await Promise.all([
    box(page.locator('.shell__bar')), box(page.locator('.shell__wordmark')),
    box(pill), box(bell), box(badge),
  ])
  const within = (inner, outer) => inner.x >= outer.x - 0.5
    && inner.y >= outer.y - 0.5
    && inner.x + inner.width <= outer.x + outer.width + 0.5
    && inner.y + inner.height <= outer.y + outer.height + 0.5
  const overlaps = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width
    && a.y < b.y + b.height && b.y < a.y + a.height
  for (const candidate of [brand, pillBox, bellBox2, badgeBox]) expect(within(candidate, header)).toBe(true)
  for (const [a, b] of [[brand, pillBox], [brand, bellBox2], [pillBox, bellBox2], [pillBox, badgeBox]]) {
    expect(overlaps(a, b)).toBe(false)
  }
})

test('phone preview stays content-sized for a short list', async ({ page }) => {
  await mockNotifications(page)
  await setup(page, { width: 390, height: 500 })
  await openPreview(page)

  const panelBox = await page.locator('.notifications').boundingBox()
  expect(panelBox.height).toBeLessThan(300)
})

test('phone preview scrolls a long list within its compact cap', async ({ page }) => {
  const manyRows = Array.from({ length: 8 }, (_, index) => ({
    id: `n-${index}`, source_type: 'agent', source_id: CHAT_ID,
    title: `Notification ${index + 1}`, body: 'A useful update with enough detail for two lines.',
    icon: null, target: `/shell/?chat=${CHAT_ID}`, actions: null,
    sent_at: new Date(Date.now() - index * 60_000).toISOString(), clicked_at: null, read_at: null,
  }))
  await mockNotifications(page, { rows: manyRows })
  await setup(page, { width: 390, height: 500 })
  await openPreview(page)

  const panel = page.locator('.notifications')
  const content = page.locator('.notifications__content')
  const panelBox = await panel.boundingBox()
  expect(panelBox.height).toBeLessThanOrEqual(352)
  const dimensions = await content.evaluate(element => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
  }))
  expect(dimensions.scrollHeight).toBeGreaterThan(dimensions.clientHeight)
})

test('preview is identical at desktop width', async ({ page }) => {
  await mockNotifications(page)
  await setup(page, { width: 1280, height: 800 })
  await openPreview(page)
  await expect(page.locator('.notifications__row')).toHaveCount(2)
})
