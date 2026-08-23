import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

function deferred() {
  let resolve
  const promise = new Promise(settle => { resolve = settle })
  return { promise, resolve }
}

test.use({ serviceWorkers: 'block' })
attachCleanup()

test('an idle runtime snapshot cannot retire an unacknowledged fresh send', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'fresh-send-runtime-race')
  expect(chat?.id).toBeTruthy()

  const sendStarted = deferred()
  const idleSnapshotReturned = deferred()
  const releaseAcknowledgement = deferred()
  let raceArmed = false
  let detailReadsAfterSend = 0

  await page.route(new RegExp(`/api/chats/${chat.id}/messages$`), async route => {
    const request = route.request().postDataJSON()
    sendStarted.resolve()
    await releaseAcknowledgement.promise
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'started',
        message: {
          role: 'user',
          content: request.content,
          blocks: [{ type: 'text', content: request.content }],
          ts: 1700001000000,
          cid: request.cid,
        },
      }),
    })
  })
  await page.route(new RegExp(`/api/chats/${chat.id}/runtime(?:\\?.*)?$`), async route => {
    if (raceArmed) await sendStarted.promise
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        running: false,
        active_goal_objective: null,
        pending_messages: [],
        pending_question_id: null,
      }),
    })
    if (raceArmed) idleSnapshotReturned.resolve()
  })
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.continue()
    if (raceArmed) detailReadsAfterSend += 1
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: chat.id,
        messages: [],
        total: 0,
        offset: 0,
        running: false,
        pending_messages: [],
        pending_question_id: null,
        provider: 'claude',
        agent_settings_json: { model: 'claude-sonnet-4-6' },
        effective_agent_settings: {
          model: 'claude-sonnet-4-6',
          effort: 'medium',
        },
      }),
    })
  })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(input).toBeVisible()

  await input.fill('Fresh send held before acknowledgement')
  raceArmed = true
  await page.keyboard.press('Enter')
  await Promise.all([sendStarted.promise, idleSnapshotReturned.promise])
  await page.evaluate(() => new Promise(resolve => {
    let frames = 8
    const next = () => (--frames ? requestAnimationFrame(next) : resolve())
    requestAnimationFrame(next)
  }))

  expect(detailReadsAfterSend).toBe(0)
  await expect(surface.locator('.chat__msg--user')).toHaveCount(1)
  await expect(surface.locator('.chat__thinking')).toBeVisible()
  await expect(surface.locator('.chat__stop')).toBeVisible()
  releaseAcknowledgement.resolve()
})
