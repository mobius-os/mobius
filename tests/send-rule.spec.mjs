/**
 * The send-scroll rule (owner's words):
 *
 *   "The first message always pins. Later messages pin only from manual
 *    auto-scroll at the bottom; every pin returns to hold."
 *
 * Direct, queued, and steered rows share that submit-time rule. Geometry alone
 * is insufficient: a chat still in PIN_USER_MSG/ANCHOR_AT is hold even if its
 * real content happens to be near the tail.
 *
 * "At bottom" is the gesture-gated follow flag, NOT a raw IO read — a
 * raw sentinel read at send time mis-classifies an at-bottom reader
 * because appending the assistant shell hides the sentinel before the
 * first follow-write. See shouldPinSend in useScrollMode.js and
 * ARCHITECTURE.md "Chat scroll + steer contract".
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
  // Let the 250ms gesture window close before the next send. Otherwise the
  // send's programmatic pin-scroll fires inside the window and the hook's
  // gesture-gated onScroll misreads it as a user gesture and flips the
  // mode away from PIN — a test-timing artifact, not real-user behavior.
  await page.evaluate(() => new Promise(r => setTimeout(r, 350)))
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

test('Send while at the bottom (following) pins to top, response grows below', async ({ page }) => {
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
  // The new message pins, then stays in hold while the long response grows.
  expect(m.lastUserVisualTop).toBeGreaterThanOrEqual(-50)
  expect(m.lastUserVisualTop).toBeLessThanOrEqual(m.clientH / 3)
  expect(m.scrollH - m.scrollTop - m.clientH).toBeGreaterThan(50)
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
// Short chat in hold — geometry alone does not authorize a second pin
// ───────────────────────────────────────────────────────────────────

test('Short chat stays in hold until the user manually enters auto-scroll', async ({ page }) => {
  await setup(page)
  await newChat(page)

  // The first send pins and therefore leaves the chat in hold. A short reply
  // does not silently turn geometric proximity into FOLLOW_BOTTOM.
  await routeStream(page, [{ type: 'catch_up_done' }, { type: 'text', content: 'Short reply.' }, { type: 'done' }])
  await sendMessage(page, 'First short')
  await waitStreamDone(page)

  // The chat fits the viewport, but no manual bottom gesture occurred.
  const fits = await measure(page)
  const fitsGap = fits.scrollH - fits.scrollTop - fits.clientH
  expect(fitsGap).toBeLessThan(50)
  const savedTop = fits.scrollTop

  await routeStream(page, [{ type: 'catch_up_done' }, { type: 'text', content: 'Another short.' }, { type: 'done' }])
  await sendMessage(page, 'Second short')
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))

  const m = await measure(page)
  expect(m.lastUserText).toBe('Second short')
  expect(Math.abs(m.scrollTop - savedTop)).toBeLessThanOrEqual(8)
  expect(m.lastUserVisualTop).toBeGreaterThan(10)
})
