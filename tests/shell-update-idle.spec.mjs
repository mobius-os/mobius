/**
 * Non-interrupting shell-update ownership.
 *
 * Rebuild and agent-finished signals may advertise one coalesced update, but
 * they never own document navigation. Chat/app navigation remains ordinary;
 * only the owner's explicit Reload shell action performs one hard navigation.
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
    undefined, { timeout: 10000 },
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
    const current = await createTaggedChat(page, 'update-current')
    // The drawer intentionally lists recent chats, not untouched drafts.
    // Give the navigation target one durable row so this test exercises the
    // ordinary drawer route rather than relying on a retired blank-draft row.
    const token = await page.evaluate(() => localStorage.getItem('token'))
    const seedTarget = await page.request.put(`${BASE}/api/chats/${target.id}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { messages: [{ role: 'user', content: 'Navigation fixture' }] },
    })
    expect(seedTarget.ok()).toBe(true)
    await page.goto(`${BASE}/shell/?chat=${current.id}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator(
      `[data-chat-id="${current.id}"][data-chat-surface="painted"]`,
    )).toBeVisible({ timeout: 8000 })
    await resetLoadCount(page)

    releaseEvents()
    await expect(page.getByRole('button', { name: /Notifications, \d+ unread/ })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Reload shell' })).toHaveCount(0)
    await page.getByRole('button', { name: /Notifications/ }).click()
    await expect(page.getByRole('button', { name: 'Reload shell' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Later' })).toBeVisible()
    // The title and supporting copy are separate inline elements in the
    // current notification, so assert their accessible text individually.
    await expect(page.getByText('New shell ready.', { exact: true })).toHaveCount(1)
    await expect(page.getByText('Reload to use the latest interface changes.', { exact: true })).toHaveCount(1)
    expect(await loadCount(page)).toBe(0)

    await page.getByRole('button', { name: 'Close notifications' }).click()
    await page.getByRole('button', { name: 'Toggle navigation' }).click()
    const targetChat = page.locator(`[data-drawer-key="chat:${target.id}"]`)
    await expect(targetChat).toBeVisible({ timeout: 8000 })
    await targetChat.click()
    await expect(page.locator(
      `[data-chat-id="${target.id}"][data-chat-surface="painted"]`,
    )).toBeVisible({ timeout: 8000 })
    expect(await loadCount(page)).toBe(0)
    await page.getByRole('button', { name: 'Notifications' }).click()
    await expect(page.getByRole('button', { name: 'Reload shell' })).toBeVisible()
    await page.getByRole('button', { name: 'Later' }).click()
    await expect(page.getByRole('heading', { name: 'Notifications' })).toHaveCount(0)
    expect(await loadCount(page)).toBe(0)
    await page.getByRole('button', { name: 'Notifications' }).click()
    await expect(page.getByRole('button', { name: 'Reload shell' })).toBeVisible()
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
    })

    releaseEvent()
    await page.getByRole('button', { name: /Notifications, \d+ unread/ }).click()
    const update = page.getByRole('button', { name: 'Reload shell' })
    await expect(update).toBeVisible()
    await update.click()

    await page.waitForFunction(
      () => Number(sessionStorage.getItem('__load_count') || '0') === 1,
      undefined, { timeout: 10000 },
    )
    await expect(page.locator(
      `[data-chat-id="${current.id}"][data-chat-surface="painted"]`,
    )).toBeVisible({ timeout: 8000 })
    expect(await page.evaluate(() => (
      sessionStorage.getItem('__before_shell_reload_seen')
    ))).toBe('1')
    expect(await loadCount(page)).toBe(1)
  })
})
