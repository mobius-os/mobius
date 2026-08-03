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

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function setupChat(page) {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
          || document.querySelector('[data-chat-surface="painted"] .chat__scroll')
          || document.querySelector('[data-chat-surface="painted"] .chat__form')),
    { timeout: 10000 }
  )
}

async function newChat(page) {
  const previousChatId = await page
    .locator('[data-chat-surface="painted"]')
    .getAttribute('data-chat-id')
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-expanded]')
    if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click()
  })
  await page.waitForFunction(
    () => !!document.querySelector('.drawer--open'),
    { timeout: 3000 }
  )
  await page.evaluate(() => {
    const newChatBtn = document.querySelector('.drawer__item--new')
    if (newChatBtn) newChatBtn.click()
  })
  await page.waitForFunction(
    () => !document.querySelector('.drawer--open'),
    { timeout: 3000 }
  )
  // New Chat presents and focuses the empty composer before its durable chat
  // allocation finishes. This test mocks a chat-id-scoped stream, so wait for
  // the new identity rather than racing the first send against allocation.
  await page.waitForFunction(previous => {
    const surface = document.querySelector('[data-chat-surface="painted"]')
    const next = surface?.getAttribute('data-chat-id')
    const composer = surface?.querySelector('[aria-label="Message Möbius…"]')
    return !!next
      && next !== previous
      && !document.querySelector('[data-new-chat-presentation]')
      && !!composer
      && !composer.disabled
  }, previousChatId, { timeout: 10000 })
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await input.press('Enter')
}

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

test.describe('handleStop sync-ordering (Ticket 034 R1)', () => {
  test('Stop with a queued message clears the queue and never resurrects it during the stop POST', async ({ page }) => {
    // Route plan:
    //   POST /messages → 202 (the optimistic queue add is local-only
    //     until the agent finishes the active turn)
    //   GET  /stream   → SSE that stays open (no `done` event) so the
    //     UI sits in sending=true with a queued tray
    //   POST /chat/stop → held for 250ms then 200 — the window the
    //     natural-handler refetch could race into
    //   GET  /chats/:id?limit=1 → returns the queue with one item
    //     ("resurrected"). If the ref-clear weren't synchronous, the
    //     fetch resolution would re-populate the tray.

    let stopHits = 0
    let refetchHits = 0
    let ordinaryMessageHits = 0
    let steerHits = 0
    let resolveStop
    const stopGate = new Promise(r => { resolveStop = r })
    let resolveSteer
    const steerGate = new Promise(r => { resolveSteer = r })

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
        // Confirm the second send as a durable queued row so the fast-forward
        // control can enter its real in-flight path.
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'queued',
            ts: 12344,
            position: 1,
            pending_message: {
              role: 'user',
              content: body.content,
              ts: 12344,
              cid: body.cid,
            },
          }),
        })
      }
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'started' }),
      })
    })
    // page.route().fulfill() cannot drip a body: it delivers the complete payload and
    // closes the response. Sleeping before fulfill merely delayed the first SSE event,
    // then closed the stream and removed Stop before the click. Install a page-local
    // fetch seam that returns a real open ReadableStream instead. All other requests
    // continue through Playwright's route mocks.
    await page.addInitScript(events => {
      const nativeFetch = window.fetch.bind(window)
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : input?.url
        if (/\/api\/chats\/[0-9a-f-]+\/stream$/.test(String(url))) {
          const encoder = new TextEncoder()
          const stream = new ReadableStream({
            start(controller) {
              for (const event of events) {
                controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
              }
            },
          })
          return Promise.resolve(new Response(stream, {
            status: 200,
            headers: {
              'Content-Type': 'text/event-stream',
              'Cache-Control': 'no-cache',
            },
          }))
        }
        return nativeFetch(input, init)
      }
    }, [
      { type: 'catch_up_done' },
      { type: 'text', content: 'streaming response...' },
    ])
    await page.route('**/api/chat/stop', async (route) => {
      stopHits++
      // Park for 250ms; any natural-handler refetch firing during
      // the await would resolve well inside this window. The
      // resurrection assertion below polls during this gap.
      await new Promise(r => setTimeout(r, 250))
      resolveStop()
      route.fulfill({
        status: 200, contentType: 'application/json', body: '{"stopped": true}',
      })
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=1$/, route => {
      refetchHits++
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          messages: [],
          offset: 0,
          provider: 'claude',
          pending_messages: [
            { role: 'user', content: 'resurrected-queue-item', ts: 12345 },
          ],
        }),
      })
    })

    await setupChat(page)
    await newChat(page)
    // Send the first message — kicks off the active turn (stream
    // stays open per the route mock above).
    await sendMessage(page, 'first message')
    // Wait until sending=true (Stop button rendered).
    await expect(page.locator('[data-chat-surface="painted"] .chat__stop')).toBeVisible({ timeout: 5000 })
    // Queue a second message while the first is still streaming.
    await sendMessage(page, 'queued message')
    // Verify the queued tray rendered with the second message.
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll('[data-chat-surface="painted"] .queued__text'))
        .some(el => el.textContent?.includes('queued message')),
      { timeout: 5000 },
    )

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

    // handleStop must:
    //   (1) bump fetchGenRef + clear pendingMessagesRef SYNCHRONOUSLY
    //   (2) then await POST /chat/stop (held by our mock for 250ms)
    // During step 2, the natural onStreamEnd path may attempt to
    // refetch; whether it does or not, the cleared queue must NOT
    // come back.
    // Poll the queued tray every ~30ms during the stop-await window.
    // Each sample must be empty (or at least not contain the
    // resurrected ts). Any sample seeing "resurrected-queue-item"
    // fails the test.
    let sawResurrection = false
    for (let i = 0; i < 8; i++) {
      const queuedTexts = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('[data-chat-surface="painted"] .queued__text'))
          .map(el => el.textContent?.trim() ?? '')
      })
      if (queuedTexts.some(t => t.includes('resurrected-queue-item'))) {
        sawResurrection = true
        break
      }
      await page.waitForTimeout(30)
    }
    await stopGate
    expect(sawResurrection, 'queue must not resurrect during stop-await').toBe(false)
    expect(stopHits).toBe(1)
    // We don't assert refetchHits — the natural handler may or may
    // not fire depending on event ordering. The load-bearing
    // contract is just "no resurrection."
  })
})
