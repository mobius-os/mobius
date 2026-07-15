/**
 * The send-scroll rule (owner's words):
 *
 *   "The first message always pins. Later messages pin when the reader is
 *    actually at the real-content bottom; every pin returns to hold."
 *
 * Direct, queued, and steered rows share that submit-time rule. The pre-append
 * real-content geometry is authoritative; internal mode can lag input/layout
 * by a frame and must not make an identical bottom send intermittent.
 *
 * Mirrors the route-mock SSE flow of second-send-pin.spec.mjs.
 *
 * Run: npx playwright test tests/send-rule.spec.mjs
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

/** Install genuinely chunked SSE responses before navigation. Atomic
 * route.fulfill bodies cannot expose the live frame between first text and
 * spacer exhaustion, which is the behavior this contract needs to observe. */
async function installChunkedStreams(page, streams) {
  await page.addInitScript((streamSpecs) => {
    const realFetch = window.fetch.bind(window)
    let streamIndex = 0
    const sentChatIds = []
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      const messageMatch = url.match(/\/api\/chats\/([^/]+)\/messages$/)
      if (messageMatch
          && String(init?.method || input?.method || 'GET').toUpperCase() === 'POST') {
        // Record synchronously. useStreamConnection opens the corresponding
        // stream only after this POST resolves, so the next matching stream
        // is unambiguously owned by this test send.
        sentChatIds.push(messageMatch[1])
        return realFetch(input, init)
      }
      const streamMatch = url.match(/\/api\/chats\/([^/]+)\/stream$/)
      if (!streamMatch) {
        return realFetch(input, init)
      }
      // Ignore foreground/reconnect streams for chats that this test did not
      // just send. This keeps the sequence deterministic even when the shell
      // initially mounts a different live chat before `newChat()` runs.
      const pendingIdx = sentChatIds.indexOf(streamMatch[1])
      if (pendingIdx < 0) return realFetch(input, init)
      sentChatIds.splice(pendingIdx, 1)
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

async function waitStreamDone(page) {
  await page.waitForFunction(() => !document.querySelector('.chat__stop'), { timeout: 10000 })
  await page.evaluate(() => new Promise(r => setTimeout(r, 300)))
}

/** Engage FOLLOW_BOTTOM the way the user does: a gesture (pointerdown)
 *  then a scroll to the bottom WITHIN the 250ms gesture window, so the
 *  hook's gesture-gated onScroll transitions the mode to FOLLOW_BOTTOM.
 *  (Mirrors spacer.spec.mjs tests 18/24.) */
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
  // Deliberately do not wait for the gesture window to expire. A real reader
  // can reach the tail and send immediately; the app must not mistake its own
  // ensuing pin write for a second reader scroll and cancel the pin.
}

/** Scroll up to read — a gesture (pointerdown) + scroll to the middle
 *  WITHIN the gesture window, so the hook transitions the mode to
 *  ANCHOR_AT (the "user is reading" state). */
async function gestureScrollUp(page) {
  await page.evaluate(() => {
    const s = document.querySelector('.chat__scroll')
    if (!s) return
    s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    s.scrollTop = Math.floor(s.scrollHeight / 3)
  })
  // Close the 250ms gesture window so ANCHOR_AT is the settled mode and a
  // subsequent send reads a stable "scrolled up" state.
  await page.evaluate(() => new Promise(r => setTimeout(r, 350)))
}

async function measure(page) {
  return page.evaluate(() => {
    const scroll = document.querySelector('.chat__scroll')
    const users = document.querySelectorAll('.chat__msg--user')
    const last = users[users.length - 1]
    if (!scroll) return { error: 'missing scroll element' }
    const sr = scroll.getBoundingClientRect()
    const lr = last?.getBoundingClientRect()
    const textEl = last?.querySelector('.chat__text--user')
    const spacer = document.querySelector('.spacer-dynamic')
    return {
      scrollTop: Math.round(scroll.scrollTop),
      clientH: scroll.clientHeight,
      scrollH: scroll.scrollHeight,
      spacerH: parseInt(spacer?.style.height) || 0,
      lastUserVisualTop: lr ? Math.round(lr.top - sr.top) : null,
      lastUserText: textEl?.textContent?.trim() ?? '',
      userMsgCount: users.length,
    }
  })
}

// ───────────────────────────────────────────────────────────────────
// First message — always pins
// ───────────────────────────────────────────────────────────────────

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

test('First message in a chat pins to the viewport top', async ({ page }) => {
  await setup(page)
  await newChat(page)
  await routeStream(page, [{ type: 'catch_up_done' }, { type: 'text', content: 'Hi.' }, { type: 'done' }])
  await sendMessage(page, 'My first message')
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))

  const m = await measure(page)
  expect(m.userMsgCount).toBe(1)
  expect(m.lastUserText).toBe('My first message')
  // Pinned flush to the top.
  expect(m.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(m.lastUserVisualTop).toBeLessThanOrEqual(10)
})

// ───────────────────────────────────────────────────────────────────
// Send while AT THE BOTTOM (following) — pins
// ───────────────────────────────────────────────────────────────────

test('Send while at the bottom hands off after a long response fills the reservation', async ({ page }) => {
  await setup(page)
  await newChat(page)

  // Long first response so the chat overflows and a scroll position
  // genuinely exists (the short-chat shortcut must not be what makes
  // this pass — the user is following).
  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'First response paragraph. '.repeat(120) },
    { type: 'done' },
  ])
  await sendMessage(page, 'First user message')
  await waitStreamDone(page)

  // Overflowing content confirmed, then the user gestures to the bottom
  // → FOLLOW_BOTTOM. Now a second send should pin.
  const overflow = await measure(page)
  expect(overflow.scrollH).toBeGreaterThan(overflow.clientH)
  await gestureToBottom(page)

  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Second response paragraph. '.repeat(120) },
    { type: 'done' },
  ])
  await sendMessage(page, 'Second from bottom')
  await waitStreamDone(page)

  const m = await measure(page)
  expect(m.userMsgCount).toBe(2)
  expect(m.lastUserText).toBe('Second from bottom')
  // This response is deliberately taller than the reserved room, so the
  // initial pin has handed off and the real response tail is now followed.
  expect(m.spacerH).toBeLessThanOrEqual(1)
  expect(m.lastUserVisualTop).toBeLessThan(0)
  expect(m.scrollH - m.scrollTop - m.clientH).toBeLessThanOrEqual(8)
})

test('Immediate tail-to-send holds through reserved streaming room, then follows', async ({ page }) => {
  await installChunkedStreams(page, [
    [
      [0, { type: 'catch_up_done' }],
      [30, { type: 'text', content: 'First response paragraph. '.repeat(120) }],
      [60, { type: 'done' }],
    ],
    [
      [0, { type: 'catch_up_done' }],
      [60, { type: 'text', content: 'HOLD_MARKER' }],
      [1800, { type: 'text', content: ' HOLD_AFTER_MANUAL_TAIL' }],
      // Leave a wide observation window after the marker. The test waits on
      // rendered text, not this duration; the later event only advances the
      // stream into the filled-reservation phase.
      [3500, { type: 'text', content: ' FILL_MARKER '.repeat(1000) }],
      [3700, { type: 'done' }],
    ],
  ])
  await setup(page)
  await newChat(page)

  await sendMessage(page, 'First user message')
  await waitStreamDone(page)
  await gestureToBottom(page)

  // No grace period after the gesture: send exactly as a person can.
  await sendMessage(page, 'Second immediately from tail')
  await page.waitForFunction(() =>
    [...document.querySelectorAll('.chat__msg--assistant')]
      .some(el => el.textContent?.includes('HOLD_MARKER')),
  null, { timeout: 5000 })

  const held = await measure(page)
  expect(held.spacerH).toBeGreaterThan(1)
  expect(held.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(held.lastUserVisualTop).toBeLessThanOrEqual(10)

  // Reproduce the owner's manual recovery exactly: move away, then reach the
  // physical tail while reserved room still exists. The next streamed chunk
  // must keep the prompt parked; it cannot turn that tail gesture into
  // immediate real-content following.
  await page.evaluate(() => {
    const s = document.querySelector('.chat__scroll')
    if (!s) return
    s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    s.scrollTop = Math.max(0, s.scrollTop - 120)
    s.scrollTop = s.scrollHeight
  })
  await page.waitForFunction(() =>
    [...document.querySelectorAll('.chat__msg--assistant')]
      .some(el => el.textContent?.includes('HOLD_AFTER_MANUAL_TAIL')))
  const manuallyHeld = await measure(page)
  expect(manuallyHeld.spacerH).toBeGreaterThan(1)
  expect(manuallyHeld.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(manuallyHeld.lastUserVisualTop).toBeLessThanOrEqual(10)

  await page.waitForFunction(() => {
    const text = [...document.querySelectorAll('.chat__msg--assistant')]
      .map(el => el.textContent || '').join(' ')
    return (text.match(/FILL_MARKER/g) || []).length > 500
  })
  await page.waitForFunction(() => {
    const scroll = document.querySelector('.chat__scroll')
    const spacer = document.querySelector('.spacer-dynamic')
    if (!scroll || !spacer || spacer.offsetHeight > 1) return false
    return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight <= 8
  })

  const following = await measure(page)
  expect(following.spacerH).toBeLessThanOrEqual(1)
  expect(following.scrollH - following.scrollTop - following.clientH).toBeLessThanOrEqual(8)
})

// ───────────────────────────────────────────────────────────────────
// Send while SCROLLED UP — preserves the reading anchor
// ───────────────────────────────────────────────────────────────────

test('Send while scrolled up preserves the exact reading position', async ({ page }) => {
  await setup(page)
  await newChat(page)

  // Long first response that overflows so there's a real reading position.
  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'A long first answer. '.repeat(150) },
    { type: 'done' },
  ])
  await sendMessage(page, 'First user message')
  await waitStreamDone(page)

  // The reader scrolls up to the middle (a gesture → ANCHOR_AT, the
  // "I'm reading" state).
  await gestureScrollUp(page)
  await page.evaluate(() => new Promise(r => setTimeout(r, 100)))
  const before = await measure(page)
  expect(before.scrollH).toBeGreaterThan(before.clientH)
  // Genuinely scrolled up, not near top or bottom.
  expect(before.scrollTop).toBeGreaterThan(20)
  const gapBefore = before.scrollH - before.scrollTop - before.clientH
  expect(gapBefore).toBeGreaterThan(50)
  const savedTop = before.scrollTop

  // Send the second message while scrolled up. It reserves reply room but must
  // not move the reader or infer auto-scroll from later layout.
  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Reply.' },
    { type: 'done' },
  ])
  await sendMessage(page, 'Second while reading')
  // Settle a few frames for any (unwanted) post-send layout effect.
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 120)))))

  const after = await measure(page)
  expect(after.lastUserText).toBe('Second while reading')
  expect(Math.abs(after.scrollTop - savedTop)).toBeLessThanOrEqual(8)
  expect(after.spacerH).toBeGreaterThanOrEqual(0)
})

// ───────────────────────────────────────────────────────────────────
// Short chat at its real-content tail — the second send pins too
// ───────────────────────────────────────────────────────────────────

test('Short chat at the real-content tail pins the next send', async ({ page }) => {
  await setup(page)
  await newChat(page)

  // The first send pins and its short reply leaves a permanent reservation.
  // The content itself still fits: the reader is at its real-content tail.
  await routeStream(page, [{ type: 'catch_up_done' }, { type: 'text', content: 'Short reply.' }, { type: 'done' }])
  await sendMessage(page, 'First short')
  await waitStreamDone(page)

  // The chat fits the viewport, so its real-content bottom is already visible.
  const fits = await measure(page)
  const fitsGap = fits.scrollH - fits.scrollTop - fits.clientH
  expect(fitsGap).toBeLessThan(50)

  await routeStream(page, [{ type: 'catch_up_done' }, { type: 'text', content: 'Another short.' }, { type: 'done' }])
  await sendMessage(page, 'Second short')
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))

  const m = await measure(page)
  expect(m.lastUserText).toBe('Second short')
  expect(m.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(m.lastUserVisualTop).toBeLessThanOrEqual(10)
})
