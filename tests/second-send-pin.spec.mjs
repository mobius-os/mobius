/**
 * Reproduces the user-reported bug: "I sent a message it went to the
 * top of my screen, I then sent another message and it didn't go to
 * the top of my screen."
 *
 * Existing test 10 in spacer.spec.mjs covers this with `injectContent`
 * (direct DOM injection bypassing React's messages state). The user's
 * real path goes through SSE → streamItems → promoteStreamToMessages,
 * which is structurally different. This test exercises the SSE path
 * end-to-end to catch any regression specific to that flow.
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

async function setupWithSSE(page, events, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)

  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  const sseBody = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: sseBody,
    })
  )

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
  await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 3000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))
}

async function waitStreamDone(page) {
  await page.waitForFunction(
    () => !document.querySelector('.chat__stop'),
    { timeout: 10000 }
  )
  // Settle for any post-stream effects (promoteStreamToMessages, etc.).
  await page.evaluate(() => new Promise(r => setTimeout(r, 300)))
}

async function measure(page) {
  return page.evaluate(() => {
    const scroll = document.querySelector('.chat__scroll')
    const userMsgs = document.querySelectorAll('.chat__msg--user')
    const last = userMsgs[userMsgs.length - 1]
    if (!scroll || !last) return { error: 'missing element' }
    const scrollRect = scroll.getBoundingClientRect()
    const lastRect = last.getBoundingClientRect()
    // Message text lives in `.chat__text--user`; the surrounding
    // `.chat__msg--user` also contains a timestamp child. Read just
    // the text content for assertion stability.
    const textEl = last.querySelector('.chat__text--user')
    return {
      scrollTop: scroll.scrollTop,
      scrollH: scroll.scrollHeight,
      clientH: scroll.clientHeight,
      lastUserVisualTop: lastRect.top - scrollRect.top,
      userMsgCount: userMsgs.length,
      lastUserText: textEl?.textContent?.trim() ?? '',
    }
  })
}

test('Second send through full SSE flow: new user msg pins to viewport top', async ({ page }) => {
  // Mock a long streamed response so the chat has real content
  // through React's promoteStreamToMessages path (the user's actual
  // production flow). Direct DOM injection (spacer test 10) doesn't
  // exercise this end-to-end.
  const events = [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Agent response paragraph. '.repeat(60) },
    { type: 'done' },
  ]
  await setupWithSSE(page, events)
  await newChat(page)

  // Send 1, wait for the stream + promote to settle.
  await sendMessage(page, 'First user message')
  await waitStreamDone(page)

  // Scroll somewhere in the middle of the response (simulating the
  // user reading) before sending the next message. This tests the
  // bug shape the user reported: scroll-state-from-prior-response
  // must NOT prevent the second send from pinning.
  await page.evaluate(() => {
    const s = document.querySelector('.chat__scroll')
    if (s) s.scrollTop = Math.floor(s.scrollHeight / 2)
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

  // Send 2.
  await sendMessage(page, 'Second user message')
  // No waitStreamDone here — pin happens immediately on send (the
  // spacer effect's `if (isSend) scrollEl.scrollTop = st`).
  // Settle a couple of frames for any post-send layout effects.
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))

  const afterSecond = await measure(page)
  expect(afterSecond.userMsgCount).toBe(2)
  expect(afterSecond.lastUserText).toBe('Second user message')

  // CRITICAL: the second user message must be visible at the top of
  // the viewport. This is the load-bearing UX the user has described
  // as "holy" — sending a message scrolls it to the top so the
  // agent's response appears below it. The threshold is loose
  // (anywhere in the top 1/3 of viewport) so we don't false-fail on
  // a few pixels of layout drift; the BUG is when this position is
  // somewhere in the lower 2/3 or off-screen, indicating no pin.
  expect(afterSecond.lastUserVisualTop).toBeGreaterThanOrEqual(-50)
  expect(afterSecond.lastUserVisualTop).toBeLessThanOrEqual(afterSecond.clientH / 3)
})

test('Pin HOLDS when content above the pinned message grows after send (late image/error/question layout)', async ({ page }) => {
  // The user-reported "first send works, subsequent can fail" bug. On a later
  // send the message pins to the top, but then content ABOVE it grows — a
  // prior turn's image finishes loading, or an error/question card renders —
  // shifting the pinned message's offsetTop with no user action. The
  // May-2026 identity-gate (useScrollMode `maybeApplyMode`, commit 47ed8b4)
  // re-applies a mode only when the mode OBJECT changes, so the steady-state
  // PIN_USER_MSG is never re-pinned and the message drifts off the top. This
  // reproduces it by growing content above the pinned message after the pin.
  const events = [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Agent response paragraph. '.repeat(60) },
    { type: 'done' },
  ]
  await setupWithSSE(page, events)
  await newChat(page)

  await sendMessage(page, 'First user message')
  await waitStreamDone(page)
  await sendMessage(page, 'Second user message')
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))

  const pinned = await measure(page)
  expect(pinned.lastUserText).toBe('Second user message')
  // Precondition: it IS pinned to the top right after send.
  expect(pinned.lastUserVisualTop).toBeLessThanOrEqual(pinned.clientH / 3)

  // Simulate late content growth ABOVE the pinned message (image load /
  // error / question card rendering in an earlier turn) — no user action.
  await page.evaluate(() => {
    const list = document.querySelector('.chat__list')
    const firstMsg = list?.querySelector('.chat__msg')
    if (firstMsg) {
      const grow = document.createElement('div')
      grow.style.height = '500px'
      grow.setAttribute('data-test-late-grow', '1')
      firstMsg.appendChild(grow)
    }
  })
  // Let the ResizeObserver fire + any re-pin settle.
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 150)))
  ))

  const afterGrow = await measure(page)
  // The pinned message must STILL be near the top. Without the
  // re-pin-on-offsetTop-change fix, lastUserVisualTop drifts down by ~500px.
  expect(afterGrow.lastUserText).toBe('Second user message')
  expect(afterGrow.lastUserVisualTop).toBeGreaterThanOrEqual(-50)
  expect(afterGrow.lastUserVisualTop).toBeLessThanOrEqual(afterGrow.clientH / 3)
})
