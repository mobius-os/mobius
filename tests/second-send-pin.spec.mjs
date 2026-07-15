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
const STREAM_ROUTE = /\/api\/chats\/[0-9a-f-]+\/stream$/

async function replaceStreamRoute(page, events) {
  await page.unroute(STREAM_ROUTE)
  const sseBody = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
  await page.route(STREAM_ROUTE, route =>
    route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: sseBody,
    })
  )
}

async function setupWithSSE(page, events, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)

  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  await replaceStreamRoute(page, events)

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

/** Engage FOLLOW_BOTTOM via a real gesture (pointerdown + scroll to the
 *  bottom inside the 250ms gesture window) — the send rule pins a
 *  subsequent send only when the user is at the bottom (following) or it
 *  is the first message. Mirrors spacer.spec.mjs tests 18/24. */
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
  // Deliberately do not wait out the gesture window. Scroll-to-tail followed
  // immediately by Send is a real interaction, not a test artifact.
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

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

test('Second send from auto-scroll pins to viewport top through the full SSE flow', async ({ page }) => {
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

  // The send rule pins a subsequent send only when the user is at the
  // bottom (following). Engage FOLLOW_BOTTOM with a real gesture before
  // the second send so this still asserts the pin under the new rule —
  // this is the "user is following the stream and sends again" case,
  // which the owner wants to keep pinning. (A scrolled-up send is the
  // opposite case, covered by send-rule.spec.mjs.)
  await gestureToBottom(page)

  // Keep the second reply shorter than the reservation so this assertion
  // observes the PIN phase. Reusing the long first-turn body here delivered
  // the whole answer atomically, legitimately exhausted the spacer, and then
  // contradicted the contract by still expecting the prompt at the top.
  await replaceStreamRoute(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Short second reply.' },
    { type: 'done' },
  ])

  // Send 2 immediately after reaching the tail.
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
  // Be at the bottom (following) so the second send pins under the rule —
  // this test is about the pin HOLDING through later content growth, so
  // it must legitimately pin first.
  await gestureToBottom(page)
  await replaceStreamRoute(page, [
    { type: 'catch_up_done' },
    { type: 'text', content: 'Short second reply.' },
    { type: 'done' },
  ])
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

test('Second send pins and HOLDS through a thinking pause when the server ts differs from the optimistic ts (F1)', async ({ page }) => {
  // The deterministic "2nd-and-later send never pins" bug. Every existing spec
  // mocked POST /messages with an EMPTY body, so startedMessagesFromResponse
  // returned null and the ts-swap RETARGET never fired — which is exactly how
  // F1 shipped. Here the server assigns its OWN ts (distinct from the optimistic
  // Date.now()), arming the retarget, and the SSE withholds content for >1s so
  // no ResizeObserver firing can mask a stranded pin. The old retarget collapsed
  // the spacer to 0px, clamping scrollTop to 0 with nothing to recover it
  // through the quiet window. The pin must hold at pinGap ≈ PIN_OFFSET (4)
  // through the pause AND after content finally streams.
  await page.setViewportSize({ width: 412, height: 915 })

  // Echo the sent content with a server ts. The echoed content keeps the SAME
  // last message across the replace-optimistic commit, so sameMessageList skips
  // the re-render — no layout effect runs to restore a collapsed spacer (the
  // load-bearing detail the bug depended on).
  let serverTsCounter = 0
  const postedCids = []
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
    const req = route.request().postDataJSON() || {}
    // Wire regression guard: every fresh send must carry its minted cid on
    // the POST — without it the durable row derives legacy-<ts> and the
    // strict data-cid pin selector goes blind after the ack (the gap the
    // adversarial review caught: the optimistic pin worked while the wire
    // silently dropped the identity).
    postedCids.push(req.cid ?? null)
    const serverTs = 1900000000000 + (serverTsCounter++)
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'started',
        // Echo the cid exactly as the real backend persists + returns it.
        message: {
          role: 'user', content: req.content ?? '', ts: serverTs,
          ...(req.cid ? { cid: req.cid } : {}),
        },
      }),
    })
  })
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' }))

  // First send streams immediately (just to build content + depth). The SECOND
  // send gets the thinking pause: the SSE is held open >1s after the POST (and
  // the retarget) resolve, before any content arrives.
  let streamCall = 0
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
    const n = streamCall++
    if (n >= 1) await new Promise(r => setTimeout(r, 1300))
    const events = n === 0
      ? [
          { type: 'catch_up_done' },
          { type: 'text', content: 'First response paragraph. '.repeat(60) },
          { type: 'done' },
        ]
      : [
          { type: 'catch_up_done' },
          { type: 'text', content: 'Second response arrives after the pause.' },
          { type: 'done' },
        ]
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: events.map(e => `data: ${JSON.stringify(e)}\n\n`).join(''),
    })
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 })
  await newChat(page)

  await sendMessage(page, 'First user message')
  await waitStreamDone(page)
  // Be at the bottom (following) so the second send legitimately pins.
  await gestureToBottom(page)

  // Send 2. The POST resolves fast (retarget fires); the SSE pauses ~1.3s.
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill('Second user message')
  await page.keyboard.press('Enter')

  // DURING the pause: wait for the optimistic row to render (POST + retarget
  // land well under the 1.3s stream delay), then assert the pin held with no
  // streamed content on screen yet.
  await page.waitForFunction(() => {
    const users = document.querySelectorAll('.chat__msg--user')
    const last = users[users.length - 1]
    return !!last && (last.querySelector('.chat__text--user')?.textContent || '')
      .includes('Second user message')
  }, { timeout: 3000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))))

  const duringPause = await measure(page)
  expect(duringPause.lastUserText).toBe('Second user message')
  // pinGap ≈ PIN_OFFSET (4). The bug strands the row at scrollTop 0 (pinGap =
  // the full offset — deep down the viewport or off the top).
  expect(duringPause.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(duringPause.lastUserVisualTop).toBeLessThanOrEqual(12)

  // Every send carried its minted cid on the wire (see route guard above).
  expect(postedCids.length).toBeGreaterThanOrEqual(2)
  for (const c of postedCids) expect(c).toBeTruthy()

  // After content finally streams in, the pin still holds.
  await waitStreamDone(page)
  const afterContent = await measure(page)
  expect(afterContent.lastUserText).toBe('Second user message')
  expect(afterContent.lastUserVisualTop).toBeGreaterThanOrEqual(-2)
  expect(afterContent.lastUserVisualTop).toBeLessThanOrEqual(12)
})
