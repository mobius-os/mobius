/**
 * The send-scroll rule (owner's words):
 *
 *   "The first message always goes to the top. Later messages go to the top
 *    only in autoscroll mode — i.e. at the physical bottom."
 *
 * Direct, queued, and steered rows share that submit-time rule. The pre-append
 * physical-tail geometry is authoritative while mode settles. Reserved reply
 * room is part of that range: scrolling upward through it exits autoscroll.
 *
 * Mirrors the route-mock SSE flow of second-send-pin.spec.mjs.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/send-rule.spec.mjs
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
  await page.waitForFunction(
    () => !document.querySelector('[data-new-chat-presentation]'),
    { timeout: 10000 },
  )
}

async function sendMessage(page, text) {
  const input = page.locator('[data-chat-surface="painted"]')
    .getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  await expect(page.locator('[data-chat-surface="painted"] .chat__scroll')).toBeVisible({ timeout: 3000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))
}

async function waitStreamDone(page) {
  await page.waitForFunction(() => !document.querySelector('[data-chat-surface="painted"] .chat__stop'), { timeout: 10000 })
  await page.evaluate(() => new Promise(r => setTimeout(r, 300)))
}

/** Engage FOLLOW_BOTTOM the way the user does: a gesture (pointerdown)
 *  then a scroll to the bottom WITHIN the 250ms gesture window, so the
 *  hook's gesture-gated onScroll transitions the mode to FOLLOW_BOTTOM.
 *  (Mirrors spacer.spec.mjs tests 18/24.) */
async function gestureToBottom(page) {
  await page.evaluate(() => {
    const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    if (s) s.scrollTop = s.scrollHeight
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
  await page.evaluate(() => {
    const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
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
    const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
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
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    const users = document.querySelectorAll('[data-chat-surface="painted"] .chat__msg--user')
    const last = users[users.length - 1]
    if (!scroll) return { error: 'missing scroll element' }
    const sr = scroll.getBoundingClientRect()
    const lr = last?.getBoundingClientRect()
    const textEl = last?.querySelector('.chat__text--user')
    const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
    return {
      scrollTop: Math.round(scroll.scrollTop),
      clientH: scroll.clientHeight,
      scrollH: scroll.scrollHeight,
      spacerH: parseInt(spacer?.style.height) || 0,
      mode: scroll.dataset.scrollMode || null,
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

test('Immediate tail-to-send follows output after the reader returns to the physical tail', async ({ page }) => {
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
    [...document.querySelectorAll('[data-chat-surface="painted"] .chat__msg--assistant')]
      .some(el => el.textContent?.includes('HOLD_MARKER')),
  null, { timeout: 5000 })

  const held = await measure(page)
  expect(held.spacerH).toBeGreaterThan(1)
  expect(held.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(held.lastUserVisualTop).toBeLessThanOrEqual(10)

  // Move away, then deliberately return to the one physical tail while
  // reserved room still exists. That reader gesture explicitly engages
  // following; until output consumes the room, the prompt remains parked at
  // the same physical tail.
  await page.evaluate(() => {
    const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    if (!s) return
    s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    s.scrollTop = Math.max(0, s.scrollTop - 120)
    s.scrollTop = s.scrollHeight
  })
  await page.waitForFunction(() =>
    [...document.querySelectorAll('[data-chat-surface="painted"] .chat__msg--assistant')]
      .some(el => el.textContent?.includes('HOLD_AFTER_MANUAL_TAIL')))
  await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
    .toHaveAttribute('data-scroll-mode', 'FOLLOW_BOTTOM')
  const followingReservedTail = await measure(page)
  expect(followingReservedTail.spacerH).toBeGreaterThan(1)
  expect(followingReservedTail.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(followingReservedTail.lastUserVisualTop).toBeLessThanOrEqual(10)

  await page.waitForFunction(() => {
    const text = [...document.querySelectorAll('[data-chat-surface="painted"] .chat__msg--assistant')]
      .map(el => el.textContent || '').join(' ')
    return (text.match(/FILL_MARKER/g) || []).length > 500
  })
  await page.waitForFunction(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
    return !!scroll && !!spacer && spacer.offsetHeight <= 1
  })

  const filled = await measure(page)
  expect(filled.spacerH).toBeLessThanOrEqual(1)
  expect(filled.scrollTop - followingReservedTail.scrollTop).toBeGreaterThan(50)
  expect(filled.scrollH - filled.scrollTop - filled.clientH).toBeLessThanOrEqual(8)
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

test('Scrolling upward inside reserved reply room keeps the next send in place', async ({ page }) => {
  await setup(page)
  await newChat(page)

  // Build real history so a later pinned row can move from the top into the
  // middle while the latest-turn reservation still remains below it.
  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Long first response. '.repeat(150) },
    { type: 'done' },
  ])
  await sendMessage(page, 'First user message')
  await waitStreamDone(page)
  await gestureToBottom(page)

  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Short second reply.' },
    { type: 'done' },
  ])
  await sendMessage(page, 'Second message pins from autoscroll')
  await waitStreamDone(page)

  const pinned = await measure(page)
  expect(pinned.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(pinned.lastUserVisualTop).toBeLessThanOrEqual(10)
  expect(pinned.spacerH).toBeGreaterThan(100)

  // This is the owner-reported failure: move upward only inside the reserved
  // range, leaving the latest user row around mid-screen. The old send rule
  // subtracted spacerH and still called this "at the bottom."
  await page.evaluate(() => {
    const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    if (!s) return
    s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    s.scrollTop = Math.max(0, s.scrollTop - Math.round(s.clientHeight * 0.45))
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 350)))

  const before = await measure(page)
  expect(before.mode).toBe('ANCHOR_AT')
  expect(before.lastUserVisualTop).toBeGreaterThan(before.clientH * 0.25)
  expect(before.lastUserVisualTop).toBeLessThan(before.clientH * 0.7)
  const physicalGap = before.scrollH - before.scrollTop - before.clientH
  const oldSpacerExcludedGap = physicalGap - before.spacerH
  expect(physicalGap).toBeGreaterThan(100)
  expect(oldSpacerExcludedGap).toBeLessThan(50)
  const savedTop = before.scrollTop

  await routeStream(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Third reply stays below the reader.' },
    { type: 'done' },
  ])
  await sendMessage(page, 'Third message while reading reserved range')
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 120)))))

  const after = await measure(page)
  expect(after.lastUserText).toBe('Third message while reading reserved range')
  expect(Math.abs(after.scrollTop - savedTop)).toBeLessThanOrEqual(8)
})

// ───────────────────────────────────────────────────────────────────
// Short chat already at the one physical tail — the second send pins too
// ───────────────────────────────────────────────────────────────────

test('Short chat at the physical tail pins the next send', async ({ page }) => {
  await setup(page)
  await newChat(page)

  // The first send pins and its short reply leaves a permanent reservation.
  // The exact spacer keeps that pin at the one physical clamp.
  await routeStream(page, [{ type: 'catch_up_done' }, { type: 'text', content: 'Short reply.' }, { type: 'done' }])
  await sendMessage(page, 'First short')
  await waitStreamDone(page)

  // The chat fits the viewport and already rests at the physical bottom.
  const fits = await measure(page)
  const fitsGap = fits.scrollH - fits.scrollTop - fits.clientH
  expect(Math.abs(fitsGap)).toBeLessThanOrEqual(4)

  await routeStream(page, [{ type: 'catch_up_done' }, { type: 'text', content: 'Another short.' }, { type: 'done' }])
  await sendMessage(page, 'Second short')
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))

  const m = await measure(page)
  expect(m.lastUserText).toBe('Second short')
  expect(m.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(m.lastUserVisualTop).toBeLessThanOrEqual(10)
})
