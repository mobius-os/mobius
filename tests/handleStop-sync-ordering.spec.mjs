/**
 * Locks in the Stop-chat contract (ARCHITECTURE.md): Stop only interrupts.
 *
 *   The server holds the queue; Stop neither clears nor resends it. While the
 *   `/chat/stop` await is open the SSE stream closes server-side, which fires
 *   the natural onStreamEnd path. That path must not refetch (a stale page
 *   could overwrite the partial Stop promotes), and the queued row must stay
 *   visible throughout. After Stop the composer stays empty and offers
 *   "Send queued messages", whose empty send runs the whole queue.
 *
 * The companion unit suites cover the pieces in isolation (composerSubmission,
 * queuedMessages' PrimaryAction, and the backend test_chat_stop_holds_queue).
 * This spec verifies the wiring end-to-end through ChatView's real handleStop
 * and the bundler output.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/handleStop-sync-ordering.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { mockLiveRuntime } from './_mockLiveRuntime.mjs'
import { attachCleanup } from './_chatTracker.mjs'
import { mockDeliveryReady, runtimeSnapshot, testChatAgentSettings } from './_chatTestPrerequisites.mjs'
import { installControlledChatStream } from './_controlledChatStream.mjs'
import { createChat, sendMessage, waitForChatShell, waitForComposerSendable } from './_chatSession.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function setupChat(page) {
  await page.setViewportSize({ width: 412, height: 915 })
  await mockDeliveryReady(page)
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await waitForChatShell(page)
}

// A send into a live turn is queued, so it adds no transcript row to wait on.
async function queueMessage(page, text) {
  const surface = page.locator('[data-chat-surface="painted"]')
  await surface.getByRole('textbox', { name: 'Message Möbius…' }).fill(text)
  await waitForComposerSendable(surface)
  await page.keyboard.press('Enter')
}

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })
attachCleanup()

test.describe('handleStop keeps the queue for the owner', () => {
  test('Stop with a queued message keeps it queued, resends nothing, and an empty Send runs it', async ({ page }) => {
    let stopHits = 0
    let stopHeld = false
    const staleRefetches = []
    let ordinaryMessageHits = 0
    let queueSends = 0
    let steerHits = 0
    let resolveStop
    const stopGate = new Promise(r => { resolveStop = r })
    let resolveSteer
    const steerGate = new Promise(r => { resolveSteer = r })
    const live = await mockLiveRuntime(page)
    const stream = await installControlledChatStream(page, 'streaming response...')
    let firstMessage
    let queuedMessage

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
      const request = route.request()
      const body = request.postDataJSON()
      if (body.force_steer) {
        steerHits++
        await steerGate
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'not_steered' }),
        })
      }
      if (body.continuation === 'queue') {
        queueSends++
        expect(body.content).toBe('')
        const promoted = { ...queuedMessage, ts: 12346 }
        delete promoted.held
        live.pending = []
        live.messages.push(promoted)
        live.running = true
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'started',
            message: { ...promoted, _consumed_cids: [queuedMessage.cid], _messages: [promoted] },
          }),
        })
      }
      ordinaryMessageHits++
      if (ordinaryMessageHits === 2) {
        queuedMessage = { role: 'user', content: body.content, ts: 12344, cid: body.cid }
        live.pending = [queuedMessage]
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'queued',
            ts: 12344,
            position: 1,
            pending_message: queuedMessage,
          }),
        })
      }
      live.running = true
      const message = { role: 'user', content: body.content, cid: body.cid, ts: 12342 + ordinaryMessageHits }
      if (!firstMessage) firstMessage = message
      live.messages.push(message)
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'started', message }),
      })
    })
    await page.route('**/api/chat/stop', async (route) => {
      stopHits++
      stopHeld = true
      await stopGate
      stopHeld = false
      // The server held the queue: the row stays, marked for the owner.
      live.running = false
      live.pending = [{ ...queuedMessage, held: true }]
      await route.fulfill({
        status: 200, contentType: 'application/json',
        json: { stopped: true, cleared_pending_cids: [] },
      })
    })

    await setupChat(page)
    const chat = await createChat(page, 'handleStop')
    await page.route(url => url.pathname === `/api/chats/${chat.id}` && url.searchParams.has('limit'), route => {
      if (!stopHeld) return route.fallback()
      // Record rather than assert here, so an unexpected refetch fails the
      // test below with its URL instead of throwing inside the route handler.
      staleRefetches.push(route.request().url())
      return route.fallback()
    })
    await stream.arm()
    await sendMessage(page, 'first message')
    await expect(page.locator('[data-chat-surface="painted"] .chat__stop')).toBeVisible({ timeout: 5000 })
    await queueMessage(page, 'queued message')
    const surface = page.locator('[data-chat-surface="painted"]')
    await expect(surface.locator('.queued__text')).toHaveText('queued message')

    // Queued work replaces Stop with Steer; a Steer in flight reveals Stop.
    await surface.locator('.chat__steer').click()
    await expect.poll(() => steerHits).toBe(1)
    const stop = surface.locator('.chat__stop')
    await expect(stop).toBeVisible()
    await stop.click()
    resolveSteer()
    await expect.poll(() => stopHits).toBe(1)

    // Natural completion lands while the Stop response is held. The queued
    // row stays visible, and the stream end requests no stale page.
    await stream.emit({ type: 'text', content: ' terminal chunk' })
    await stream.finish()
    live.messages.push({ role: 'assistant', content: 'streaming response... terminal chunk' })
    await expect(surface.locator('.chat__scroll')).toContainText('terminal chunk')
    await expect(surface.locator('.queued__text')).toHaveText('queued message')
    expect(stopHeld).toBe(true)
    expect(staleRefetches).toEqual([])

    resolveStop()
    const sendQueued = surface.getByRole('button', { name: 'Send queued messages' })
    await expect(sendQueued).toBeVisible()
    await expect(surface.locator('.queued__text')).toHaveText('queued message')
    await expect(surface.getByRole('textbox', { name: 'Message Möbius…' })).toHaveValue('')
    // Stop resent nothing.
    expect(ordinaryMessageHits).toBe(2)
    expect(queueSends).toBe(0)

    await sendQueued.click()
    await expect.poll(() => queueSends).toBe(1)
    await expect(surface.locator('.chat__msg--user').last()).toContainText('queued message')
    await expect(surface.locator('.queued__row')).toHaveCount(0)
    expect(ordinaryMessageHits).toBe(2)
  })
})
