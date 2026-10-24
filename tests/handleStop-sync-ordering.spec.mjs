/**
 * Locks in the R1 invariant from .pm/features/_034-design.md:
 *
 *   handleStop must clear the pending-queue ref SYNCHRONOUSLY before
 *   the `/chat/stop` await. During that await the SSE stream closes
 *   server-side (kill proc + close broadcast), which fires the natural
 *   onStreamEnd path in useStreamConnection → ChatView's onStreamEnd →
 *   if pendingMessagesRef has items it would call fetchMessages with
 *   force:true → that fetch resolving BEFORE handleStop continues
 *   post-await would overwrite the just-promoted partial + the
 *   soon-to-be-sent combined turn with stale DB state.
 *
 * Originated by Ticket 034. Lives in tests/ alongside the other
 * Playwright lock-ins so it runs in the same suite.
 *
 * The companion unit suite at
 *   frontend/src/components/ChatView/hooks/__tests__/usePendingQueue.test.js
 * covers the hook contract in isolation (clear / cancelByTs / hydrate
 * all update pendingMessagesRef.current synchronously). This spec
 * verifies that contract holds end-to-end through ChatView's real
 * handleStop + the bundler output, catching wiring regressions that
 * the node-side unit suite cannot see.
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

test.describe('handleStop sync-ordering (Ticket 034 R1)', () => {
  test('Stop with a queued message clears the queue and never resurrects it during the stop POST', async ({ page }) => {
    let stopHits = 0
    let stopHeld = false
    let stopReleased = false
    let refetchHits = 0
    let ordinaryMessageHits = 0
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
      ordinaryMessageHits++
      if (ordinaryMessageHits === 2) {
        queuedMessage = { role: 'user', content: body.content, ts: 12344, cid: body.cid }
        live.pending = [queuedMessage]
        // Confirm the second send as a durable queued row so the fast-forward
        // control can enter its real in-flight path.
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
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        // Echo the accepted message so the send intent retires; otherwise the
        // outbox replays it and the real second send is never confirmed as queued.
        body: JSON.stringify({
          status: 'started',
          message,
        }),
      })
    })
    await page.route('**/api/chat/stop', async (route) => {
      stopHits++
      stopHeld = true
      await stopGate
      stopHeld = false
      stopReleased = true
      live.running = false
      live.pending = []
      await route.fulfill({
        status: 200, contentType: 'application/json',
        json: { stopped: true, cleared_pending_cids: [queuedMessage.cid] },
      })
    })

    await setupChat(page)
    const chat = await createChat(page, 'handleStop')
    await page.route(url => url.pathname === `/api/chats/${chat.id}` && url.searchParams.has('limit'), route => {
      if (!stopHeld) return route.fallback()
      refetchHits++
      const url = new URL(route.request().url())
      expect(url.searchParams.get('limit')).toBe('20')
      expect(url.searchParams.get('compact')).toBe('1')
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        // This is a valid but stale pre-stop server page. Include the local
        // transcript prefix so the behind-local-history guard cannot mask a
        // broken clear-before-await guard, and a revision the app will adopt.
        json: {
          ...testChatAgentSettings(),
          ...runtimeSnapshot({ runtime_revision: 1, pending_messages: [
            { role: 'user', content: 'resurrected-queue-item', ts: 12345, cid: 'stale-queue-cid' },
          ] }),
          id: chat.id,
          messages: [firstMessage, { role: 'assistant', content: 'streaming response... terminal chunk' }],
          total: 2,
          offset: 0,
        },
      })
    })
    await stream.arm()
    // Send the first message — kicks off the active turn with an open body.
    await sendMessage(page, 'first message')
    // Wait until sending=true (Stop button rendered).
    await expect(page.locator('[data-chat-surface="painted"] .chat__stop')).toBeVisible({ timeout: 5000 })
    // Queue a second message while the first is still streaming.
    await queueMessage(page, 'queued message')
    // Verify the queued tray rendered with the second message.
    await expect(page.locator('[data-chat-surface="painted"] .queued__text')).toHaveText('queued message')

    // Queued work intentionally replaces Stop with Steer. Enter the real
    // reachable overlap: Steer hides its confirmed row while its POST is in
    // flight, which reveals Stop; Stop serializes behind that request. Resolve
    // Steer as not_steered so it restores the durable row before handleStop
    // snapshots and clears it.
    await page.locator('[data-chat-surface="painted"] .chat__steer').click()
    await expect.poll(() => steerHits).toBe(1)
    const stop = page.locator('[data-chat-surface="painted"] .chat__stop')
    await expect(stop).toBeVisible()
    await stop.click()
    resolveSteer()
    await expect.poll(() => stopHits).toBe(1)
    const surface = page.locator('[data-chat-surface="painted"]')
    await expect(surface.locator('.queued__row')).toHaveCount(0)
    await expect(stop).toBeVisible()
    expect(await stream.state()).toEqual({ requests: 1, open: true, terminals: 0, aborts: 0 })

    // Observe every queue DOM mutation during the controlled await, not just
    // the sentinel text at the end. No queued row (including the original)
    // may reappear. Keep the Stop response held until natural completion has
    // visibly retired the live action and several paint checkpoints settle.
    await page.evaluate(() => {
      const surface = document.querySelector('[data-chat-surface="painted"]')
      const samples = []
      const sample = () => samples.push(surface.querySelectorAll('.queued__row').length)
      const observer = new MutationObserver(sample)
      observer.observe(surface, { childList: true, subtree: true, characterData: true })
      sample()
      window.__stopQueueWindow = { samples, sample, observer }
    })
    // The backend can close the broadcast before replying to Stop. Its
    // runtime now says idle; only the intentionally stale detail trap above
    // can claim the cleared queue still exists.
    live.running = false
    live.pending = []
    expect(stopHeld).toBe(true)
    expect(stopReleased).toBe(false)
    await stream.emit({ type: 'text', content: ' terminal chunk' })
    await stream.finish()
    await expect(surface.locator('.chat__scroll')).toContainText('terminal chunk')
    await expect(surface.locator('.chat__mic')).toBeVisible()
    await expect(stop).toHaveCount(0)
    expect(await stream.state()).toEqual({ requests: 1, open: false, terminals: 1, aborts: 0 })
    const samples = await page.evaluate(async () => {
      const windowState = window.__stopQueueWindow
      for (let i = 0; i < 3; i++) {
        await new Promise(requestAnimationFrame)
        windowState.sample()
      }
      return windowState.samples
    })
    expect(stopHeld).toBe(true)
    expect(stopReleased).toBe(false)
    expect(samples.length).toBeGreaterThanOrEqual(4)
    expect(samples.every(count => count === 0), `queue counts during Stop await: ${samples}`).toBe(true)
    // With an ordinary non-Goal chat and a synchronously empty ref, natural
    // completion has no reason to request the stale page at all.
    expect(refetchHits).toBe(0)
    await expect(surface.locator('.queued__row')).toHaveCount(0)

    resolveStop()
    await expect.poll(() => ordinaryMessageHits).toBe(3)
    await expect(stop).toBeVisible()
    await expect(surface.locator('.chat__msg--user').last()).toContainText('queued message')
    await expect(surface.locator('.queued__row')).toHaveCount(0)
    const allSamples = await page.evaluate(() => {
      const windowState = window.__stopQueueWindow
      windowState.sample()
      windowState.observer.disconnect()
      return windowState.samples
    })
    expect(allSamples.every(count => count === 0), `queue counts through Stop response: ${allSamples}`).toBe(true)
  })
})
