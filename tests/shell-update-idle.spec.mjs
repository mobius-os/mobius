/**
 * Non-interrupting shell-update ownership.
 *
 * Rebuild and agent-finished signals may advertise one coalesced update, but
 * they never own document navigation. Chat/app navigation remains ordinary;
 * only the owner's explicit Update now action performs one hard navigation.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/shell-update-idle.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

attachCleanup()

function sse(events) {
  return events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('')
}

function fulfillStream(body) {
  return {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
    body,
  }
}

function oneShotSystemEventsRoute(eventTypes, armed) {
  let delivered = false
  return async (route) => {
    try {
      if (!delivered) {
        await armed
        if (!delivered) {
          await route.fulfill(fulfillStream(sse([
            { type: 'system_stream_open' },
            ...eventTypes.map(type => ({ type })),
          ])))
          delivered = true
          return
        }
      }
      await route.fulfill(fulfillStream(sse([{ type: 'system_stream_open' }])))
    } catch {
      // Navigation may abort the held connection. Preserve the one-shot event
      // for the next live connection, matching SystemBroadcast's no-replay edge.
    }
  }
}

async function trackLoads(page) {
  await page.addInitScript(() => {
    const next = Number(sessionStorage.getItem('__load_count') || '0') + 1
    sessionStorage.setItem('__load_count', String(next))
  })
}

const loadCount = page => page.evaluate(
  () => Number(sessionStorage.getItem('__load_count') || '0'),
)
const resetLoadCount = page => page.evaluate(
  () => sessionStorage.setItem('__load_count', '0'),
)

async function setup(page, systemRoute) {
  await page.setViewportSize({ width: 412, height: 915 })
  await trackLoads(page)
  await page.route('**/api/events/system', systemRoute)
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!document.querySelector('.shell'),
    { timeout: 10000 },
  )
}

test.describe('shell update — owner-controlled navigation', () => {
  test('many rebuild signals cannot swallow the next chat navigation', async ({ page }) => {
    let releaseEvents
    const armed = new Promise(resolve => { releaseEvents = resolve })
    await setup(page, oneShotSystemEventsRoute([
      'shell_rebuilt',
      'shell_apply_now',
      'shell_rebuilt',
      'shell_apply_now',
    ], armed))
    const target = await createTaggedChat(page, 'update-target')
    // Empty chats deliberately stay out of Drawer Recents. Seed one durable
    // transcript row so this fixture exercises a real drawer navigation
    // target instead of waiting forever for an intentionally hidden row.
    const token = await page.evaluate(() => localStorage.getItem('token'))
    const seeded = await page.request.put(`${BASE}/api/chats/${target.id}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { messages: [{ role: 'user', content: 'Navigation target' }] },
      failOnStatusCode: false,
    })
    expect(seeded.ok(), await seeded.text()).toBeTruthy()
    const current = await createTaggedChat(page, 'update-current')
    await page.goto(`${BASE}/shell/?chat=${current.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator(
      `[data-chat-id="${current.id}"][data-chat-surface="painted"]`,
    )).toBeVisible({ timeout: 8000 })
    await resetLoadCount(page)

    releaseEvents()
    await expect(page.getByRole('button', { name: 'Update now' })).toBeVisible()
    await expect(page.getByText('A Möbius update is ready.')).toHaveCount(1)
    expect(await loadCount(page)).toBe(0)

    await page.getByRole('button', { name: 'Toggle navigation' }).click()
    await page.locator(`[data-drawer-key="chat:${target.id}"]`).click()
    await expect(page.locator(
      `[data-chat-id="${target.id}"][data-chat-surface="painted"]`,
    )).toBeVisible({ timeout: 8000 })
    expect(await loadCount(page)).toBe(0)
    await expect(page.getByRole('button', { name: 'Update now' })).toBeVisible()
  })

  test('one explicit update preserves the current chat and navigates once', async ({ page }) => {
    let releaseEvent
    const armed = new Promise(resolve => { releaseEvent = resolve })
    await setup(page, oneShotSystemEventsRoute(['shell_apply_now'], armed))
    const current = await createTaggedChat(page, 'explicit-update-current')
    await page.goto(`${BASE}/shell/?chat=${current.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator(
      `[data-chat-id="${current.id}"][data-chat-surface="painted"]`,
    )).toBeVisible({ timeout: 8000 })
    await resetLoadCount(page)
    await page.evaluate(() => {
      window.addEventListener('mobius:before-shell-reload', () => {
        sessionStorage.setItem('__before_shell_reload_seen', '1')
      }, { once: true })
      window.addEventListener('pageswap', event => {
        sessionStorage.setItem(
          '__shell_reload_view_transition',
          event.viewTransition ? 'yes' : 'no',
        )
      }, { once: true })
    })

    releaseEvent()
    const update = page.getByRole('button', { name: 'Update now' })
    await expect(update).toBeVisible()
    await update.click()

    await page.waitForFunction(
      () => Number(sessionStorage.getItem('__load_count') || '0') === 1,
      { timeout: 10000 },
    )
    await expect(page.locator(
      `[data-chat-id="${current.id}"][data-chat-surface="painted"]`,
    )).toBeVisible({ timeout: 8000 })
    expect(await page.evaluate(() => (
      sessionStorage.getItem('__before_shell_reload_seen')
    ))).toBe('1')
    expect(await page.evaluate(() => (
      sessionStorage.getItem('__shell_reload_view_transition')
    ))).toBe('yes')
    await expect(page.locator('html[data-shell-reload-transition]')).toHaveCount(0)
    expect(await loadCount(page)).toBe(1)
  })
})
