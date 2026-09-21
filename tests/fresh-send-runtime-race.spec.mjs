import { test, expect } from '@playwright/test'
import { attachCleanup, createTaggedChat } from './_chatTracker.mjs'
import { testChatAgentSettings } from './_chatTestPrerequisites.mjs'

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
  // connectivityStore.js's probeReadiness() fetches /api/ready and requires
  // body.ready === true (plus a boot_id) before treating the app as
  // delivery-ready. Without this, deliveryReady stayed false through this
  // whole test (confirmed via a debug log at doSend's branch decision), so
  // doSend took the QUEUED path instead of the FRESH SEND PATH -- the one
  // that sets localStartRequestRef, the exact protection this test exists to
  // exercise. The queued path never sets it, so the race "protection" was
  // never engaged and every assertion here was accidentally vacuous.
  await page.route(/\/api\/ready$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ ready: true, boot_id: 'fresh-send-race-fixture-boot' }),
  }))
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
  // Without this, the real backend's SSE endpoint gets hit for a chat with
  // no active run (the /messages POST above is entirely intercepted and
  // never reaches the server), closes almost immediately, and onStreamEnd's
  // continues:false branch calls fetchMessages({force:true, authoritative:
  // true}) on its own -- a completely different source of the exact
  // detailReadsAfterSend this test measures, independent of the runtime-poll
  // race it's named for. Hold it open for the same window as the POST.
  await page.route(new RegExp(`/api/chats/${chat.id}/stream$`), async route => {
    await releaseAcknowledgement.promise
    await route.fulfill({ status: 204, body: '' })
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
        runtime_revision: 0,
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
        runtime_revision: 0,
        provider: 'claude',
        ...testChatAgentSettings(),
      }),
    })
  })

  const readinessProbed = page.waitForResponse(/\/api\/ready$/)
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await expect(input).toBeVisible()
  // Wait for the mocked probe AND let React commit the resulting
  // deliveryReady=true state before sending -- otherwise doSend can still
  // observe the pre-ready snapshot and take the queued path instead of the
  // fresh-send path this test depends on, even with /api/ready mocked above.
  await readinessProbed
  await page.evaluate(() => new Promise(resolve => {
    let frames = 8
    const next = () => (--frames ? requestAnimationFrame(next) : resolve())
    requestAnimationFrame(next)
  }))

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
