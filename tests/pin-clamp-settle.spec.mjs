/**
 * Reproduces the owner's #1 recurring complaint: "I sent a message and it
 * went only HALFWAY up the screen, not all the way to the top." — a
 * subsequent send pins the user message to the top, then the post-send
 * layout settle (thinking dots, streamed first token, markdown/lazy
 * renderers) momentarily shrinks the spacer / changes scrollHeight, the
 * browser CLAMPS scrollTop below the pin target, and nothing re-anchors
 * it — so the message lands a chunk below the top and stays there.
 *
 * useScrollMode applies PIN_USER_MSG exactly once (the maybeApplyMode
 * identity gate) and its ResizeObserver only re-pins when the message's
 * offsetTop SHIFTS (content grew ABOVE it). A pure scrollTop clamp from
 * a layout settle BELOW the pin leaves offsetTop unchanged, so the old
 * RO branch is a no-op and the clamp is permanent. This is the
 * clamp-fix obligation in ARCHITECTURE.md's chat-scroll contract — honored
 * for FOLLOW_BOTTOM/ANCHOR_AT but missing for PIN_USER_MSG.
 *
 * The repro: a long first response pushes the second user message DEEP
 * into the list (large offsetTop ⇒ large pin target ⇒ spacer in play),
 * then a SHORT second response means little content grows below the pin —
 * exactly the shape where the settle-time clamp is never compensated by
 * content growth and the drift is permanent.
 *
 * Invariant this locks in: after the settle, the ResizeObserver re-pins
 * PIN_USER_MSG whenever scrollTop drifts below the pin target, so the message
 * stays flush at the top instead of stranding ~24px+ down. This is the
 * clamp-fix obligation for PIN_USER_MSG — already honored for
 * FOLLOW_BOTTOM/ANCHOR_AT.
 *
 * Mirrors tests/second-send-pin.spec.mjs's route-mock SSE flow.
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function setup(page, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' }))
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' }))
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 })
}

/** Swap in an SSE response body for the next stream the app opens. */
async function routeStream(page, events) {
  const body = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body,
    }))
}

/** Install a deterministic, genuinely chunked SSE source before navigation.
 *  `page.route().fulfill({body})` delivers the whole body atomically, which
 *  cannot exercise a keyboard-close event BETWEEN the final text frame and
 *  `done`. Each array entry is one stream connection; each tuple is
 *  [delayMs, event]. Normal fetch remains untouched for every other route. */
async function installChunkedStreams(page, streams) {
  await page.addInitScript((streamSpecs) => {
    const realFetch = window.fetch.bind(window)
    let streamIndex = 0
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      if (!/\/api\/chats\/[^/]+\/stream$/.test(url)) {
        return realFetch(input, init)
      }
      const spec = streamSpecs[streamIndex++] || []
      const encoder = new TextEncoder()
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          for (const [delayMs, event] of spec) {
            setTimeout(() => {
              controller.enqueue(
                encoder.encode(`data: ${JSON.stringify(event)}\n\n`),
              )
              if (event.type === 'done') controller.close()
            }, delayMs)
          }
        },
      }), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    }
  }, streams)
}

async function newChat(page) {
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-expanded]')
    if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click()
  })
  await page.waitForFunction(() => !!document.querySelector('.drawer--open'), { timeout: 3000 })
  await page.evaluate(() => document.querySelector('.drawer__item--new')?.click())
  await page.waitForFunction(() => !document.querySelector('.drawer--open'), { timeout: 3000 })
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 3000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))
}

/** Engage FOLLOW_BOTTOM via a real gesture so a subsequent send pins
 *  under the send rule (the clamp-fix only matters on a legitimate pin).
 *  Mirrors spacer.spec.mjs tests 18/24. */
async function gestureToBottom(page) {
  await page.evaluate(() => {
    const s = document.querySelector('.chat__scroll')
    if (s) s.scrollTop = s.scrollHeight
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
  await page.evaluate(() => {
    const s = document.querySelector('.chat__scroll')
    if (!s) return
    s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    s.scrollTop = Math.max(0, s.scrollTop - 1)
    s.scrollTop = s.scrollHeight
  })
  // Let the 250ms gesture window close before the next send. Otherwise the
  // send's programmatic pin-scroll fires inside the window and the hook's
  // gesture-gated onScroll misreads it as a user gesture, flipping the
  // mode away from PIN — a test-timing artifact, not real-user behavior.
  await page.evaluate(() => new Promise(r => setTimeout(r, 350)))
}

async function waitStreamDone(page) {
  await page.waitForFunction(() => !document.querySelector('.chat__stop'), { timeout: 10000 })
  await page.evaluate(() => new Promise(r => setTimeout(r, 300)))
}

async function measure(page) {
  return page.evaluate(() => {
    const scroll = document.querySelector('.chat__scroll')
    const users = document.querySelectorAll('.chat__msg--user')
    const last = users[users.length - 1]
    if (!scroll || !last) return { error: 'missing element' }
    const sr = scroll.getBoundingClientRect()
    const lr = last.getBoundingClientRect()
    const textEl = last.querySelector('.chat__text--user')
    return {
      lastUserVisualTop: Math.round(lr.top - sr.top),
      lastUserText: textEl?.textContent?.trim() ?? '',
      userMsgCount: users.length,
    }
  })
}

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

test('Deep second send pins flush to top after the post-send layout settle (no halfway clamp)', async ({ page }) => {
  await setup(page)
  await newChat(page)

  // First response is long → the second user message lands DEEP in the
  // list (large offsetTop ⇒ pin target needs a spacer).
  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'First response line. '.repeat(90) },
    { type: 'done' },
  ])
  await sendMessage(page, 'First user message')
  await waitStreamDone(page)

  // The send rule pins a subsequent send only when the user is at the
  // bottom — engage FOLLOW_BOTTOM so the deep second send legitimately
  // pins (this test exercises the clamp-fix that keeps that pin flush at
  // the top through the post-send layout settle).
  await gestureToBottom(page)

  // Second response is SHORT — minimal content grows below the pin, so a
  // settle-time scrollTop clamp is NOT masked by content growth.
  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'OK.' },
    { type: 'done' },
  ])
  await sendMessage(page, 'Second deep message')

  // Let the post-send layout fully settle: thinking dots → streamed
  // token → promote. This is where the drift becomes permanent.
  await waitStreamDone(page)
  await page.evaluate(() => new Promise(r => setTimeout(r, 300)))

  const m = await measure(page)
  expect(m.userMsgCount).toBe(2)
  expect(m.lastUserText).toBe('Second deep message')

  // The pinned message must be flush at the top — same tight tolerance
  // as spacer.spec.mjs's assertUserMsgAtTop. The BUG lands it ~24px+
  // below the top (a permanent clamp the RO never compensates).
  expect(m.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(m.lastUserVisualTop).toBeLessThanOrEqual(10)
})

test('Keyboard close cannot retire a pin before a short stream settles', async ({ page }) => {
  await installChunkedStreams(page, [
    [
      [0, { type: 'catch_up_done' }],
      [40, { type: 'text', content: 'First response line. '.repeat(90) }],
      [400, { type: 'done' }],
    ],
    [
      [0, { type: 'catch_up_done' }],
      [60, { type: 'text', content: 'OK.' }],
      // Keep the short live frame open long enough to close the simulated
      // keyboard before terminal promotion swaps live → settled markup.
      [1500, { type: 'done' }],
    ],
  ])
  await setup(page, { width: 426, height: 860 })
  await newChat(page)

  await sendMessage(page, 'First user message')
  await waitStreamDone(page)
  await gestureToBottom(page)

  // Match the real mobile order: the composer opens the keyboard (short
  // viewport), send pins there, then blur closes the keyboard while the reply
  // is still streaming. The grow-only fullViewH reservation intentionally
  // makes the pin look away from the PHYSICAL bottom while the keyboard is
  // open; that temporary geometry must not be mistaken for reader intent.
  await page.setViewportSize({ width: 426, height: 560 })
  await sendMessage(page, 'Second deep message')
  await expect(page.locator('.chat__cursor')).toBeVisible({ timeout: 5000 })

  const pinnedWithKeyboard = await measure(page)
  expect(pinnedWithKeyboard.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(pinnedWithKeyboard.lastUserVisualTop).toBeLessThanOrEqual(10)

  await page.setViewportSize({ width: 426, height: 860 })
  const pinnedAfterKeyboardClose = await measure(page)
  expect(pinnedAfterKeyboardClose.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(pinnedAfterKeyboardClose.lastUserVisualTop).toBeLessThanOrEqual(10)

  await waitStreamDone(page)
  const settled = await measure(page)
  expect(settled.lastUserText).toBe('Second deep message')
  expect(settled.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(settled.lastUserVisualTop).toBeLessThanOrEqual(10)
})
