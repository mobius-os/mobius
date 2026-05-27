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
 * Run: npx playwright test tests/handleStop-sync-ordering.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

function sseBody(events) {
  return events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
}

async function setupChat(page) {
  await page.setViewportSize({ width: 412, height: 915 })
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 }
  )
}

async function newChat(page) {
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
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
}

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
    let resolveStop
    const stopGate = new Promise(r => { resolveStop = r })

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, contentType: 'application/json', body: '{}' })
    )
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async (route) => {
      // Hold the stream connection open for the lifetime of the test
      // so isStreaming stays true and the second send hits the QUEUE
      // path. (Playwright's route.fulfill flushes the body atomically
      // — the SSE "stream" needs to be held open externally to
      // simulate a long-running turn. Pattern lifted from
      // stream-reconnect.spec.mjs test 4.)
      await new Promise(r => setTimeout(r, 8000))
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: sseBody([
          { type: 'catch_up_done' },
          { type: 'text', content: 'streaming response...' },
        ]),
      }).catch(() => {})
    })
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
    await expect(page.locator('.chat__stop')).toBeVisible({ timeout: 5000 })
    // Queue a second message while the first is still streaming.
    await sendMessage(page, 'queued message')
    // Verify the queued tray rendered with the second message.
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll('.queued__text'))
        .some(el => el.textContent?.includes('queued message')),
      { timeout: 5000 },
    )

    // Press Stop. handleStop must:
    //   (1) bump fetchGenRef + clear pendingMessagesRef SYNCHRONOUSLY
    //   (2) then await POST /chat/stop (held by our mock for 250ms)
    // During step 2, the natural onStreamEnd path may attempt to
    // refetch; whether it does or not, the cleared queue must NOT
    // come back.
    await page.locator('.chat__stop').click()
    // Poll the queued tray every ~30ms during the stop-await window.
    // Each sample must be empty (or at least not contain the
    // resurrected ts). Any sample seeing "resurrected-queue-item"
    // fails the test.
    let sawResurrection = false
    for (let i = 0; i < 8; i++) {
      const queuedTexts = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('.queued__text'))
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
