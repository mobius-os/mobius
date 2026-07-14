/**
 * Locks in the "steer queued messages" (Codex-style fast-forward) feature.
 *
 * Background: messages sent during a running turn queue onto
 * Chat.pending_messages (the "N QUEUED" tray). Today Stop is the only
 * mid-turn flush, and it HARD-interrupts. This feature adds a
 * fast-forward button that STEERS the queued messages into the LIVE turn
 * at the next natural boundary via POST /messages with
 * `force_steer:true` + `consume_pending_cids` (backend contract in
 * routes/chats_stream.py `_selected_force_steer_pending` — cid selection,
 * no content byte-match).
 *
 * This spec mocks the network (no live backend), mirroring the
 * route-mock style of second-send-pin.spec.mjs + handleStop-sync-
 * ordering.spec.mjs. It asserts:
 *   (a) the fast-forward button appears (replacing Stop) once a message
 *       is queued AND server-confirmed during streaming,
 *   (b) pressing it POSTs force_steer:true with the right
 *       consume_pending_cids + the exact "\n\n"-joined content,
 *   (c) the queued tray clears on a {status:"steered"} response.
 *
 * Run: npx playwright test tests/steer-queued.spec.mjs
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

// The real service worker claims the page ~1s after load; from then on its
// fetch() handler bypasses page.route, so the mocked /messages + /stream
// contracts silently fall through to the real backend and the mock's steer
// events never arrive (the app-canvas spec hit the identical class). These
// tests mock the network and do not exercise SW behavior, so block it.
test.use({ serviceWorkers: 'block' })

test.describe('Steer queued messages (fast-forward into the live turn)', () => {
  test('fast-forward button appears, POSTs force_steer with the right payload, and clears the tray', async ({ page }) => {
    // The server-assigned ts the queueOnly POST hands back. The steer's
    // consume_pending_cids must equal [queued cid] and its content must equal
    // the queued message's trimmed content (single message → no join).
    const QUEUE_TS = 777001
    const QUEUED_TEXT = 'queued message to steer'

    // Capture every POST /messages so we can assert the steer payload.
    const messagePosts = []

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async (route) => {
      const req = route.request()
      let body = {}
      try { body = JSON.parse(req.postData() || '{}') } catch { /* empty */ }
      messagePosts.push(body)

      // Force-steer POST: convert the queued message into the live turn.
      // Respond exactly as the backend does on success — 202 with
      // {status:"steered"} and the remaining (now empty) server queue.
      if (body.force_steer) {
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'steered',
            chat_id: 'mock',
            pending_messages: [],
          }),
        })
      }

      // First send (fresh turn): 202 starts the turn; the held-open
      // /stream below keeps sending=true.
      if (body.content === 'first message') {
        return route.fulfill({ status: 202, contentType: 'application/json', body: '{}' })
      }

      // Second send while streaming: the queue path. Return a SERVER ts so
      // confirmQueued clears the in-flight flag — only then is the entry
      // steer-eligible (canSteer requires a confirmed server ts).
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', ts: QUEUE_TS, position: 1 }),
      })
    })

    // Hold the stream open so the turn keeps streaming (sending=true) for
    // the whole test — pattern from handleStop-sync-ordering.spec.mjs.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async (route) => {
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

    await setupChat(page)
    await newChat(page)

    // First send → starts the (held-open) turn. Stop button = streaming.
    await sendMessage(page, 'first message')
    await expect(page.locator('.chat__stop')).toBeVisible({ timeout: 5000 })

    // Queue a second message while streaming.
    await sendMessage(page, QUEUED_TEXT)
    await page.waitForFunction(
      (t) => Array.from(document.querySelectorAll('.queued__text'))
        .some(el => el.textContent?.includes(t)),
      QUEUED_TEXT,
      { timeout: 5000 },
    )

    // (a) Once the queue entry is server-confirmed (the queueOnly POST
    // returned a server ts → confirmQueued cleared the in-flight flag),
    // the Stop square is swapped for the fast-forward (steer) button.
    const steerBtn = page.getByRole('button', { name: 'Send queued message now' })
    await expect(steerBtn).toBeVisible({ timeout: 5000 })
    // Stop must be gone while steer is showing (the slot swaps, not stacks).
    await expect(page.locator('.chat__stop')).toHaveCount(0)

    // (b) Press it → expect a force_steer POST with the exact payload.
    await steerBtn.click()
    await expect.poll(
      () => messagePosts.filter(b => b.force_steer).length,
      { timeout: 5000 },
    ).toBe(1)

    const steerPost = messagePosts.find(b => b.force_steer)
    expect(steerPost.force_steer).toBe(true)
    // consume_pending_cids is exactly the queued row's stable cid (minted
    // client-side and echoed on the queue POST body).
    const queuePost = messagePosts.find(b => !b.force_steer && b.content === QUEUED_TEXT)
    expect(typeof queuePost.cid).toBe('string')
    expect(steerPost.consume_pending_cids).toEqual([queuePost.cid])
    // content is the queued message's trimmed content (single msg, no join).
    expect(steerPost.content).toBe(QUEUED_TEXT)

    // (c) The tray clears on {status:"steered"} (steered rows now render
    // inline via the steered_into_turn event; tray drops them).
    await page.waitForFunction(
      () => document.querySelectorAll('.queued__row').length === 0,
      { timeout: 5000 },
    )
    expect(await page.locator('.queued__row').count()).toBe(0)
  })

  test('two queued messages steer with the exact "\\n\\n"-joined content', async ({ page }) => {
    // Verifies the frontend content join sent to the provider steer: the
    // non-empty trimmed contents joined
    // by "\n\n", in pending order, with consume_pending_cids = both cids.
    const TS1 = 880001
    const TS2 = 880002
    const TEXT1 = 'first queued'
    const TEXT2 = 'second queued'

    const messagePosts = []
    // The queueOnly POSTs land in order, so hand back TS1 then TS2.
    let queueCount = 0

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async (route) => {
      const req = route.request()
      let body = {}
      try { body = JSON.parse(req.postData() || '{}') } catch { /* empty */ }
      messagePosts.push(body)

      if (body.force_steer) {
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'steered', chat_id: 'mock', pending_messages: [] }),
        })
      }
      if (body.content === 'first message') {
        return route.fulfill({ status: 202, contentType: 'application/json', body: '{}' })
      }
      const ts = queueCount === 0 ? TS1 : TS2
      const position = queueCount + 1
      queueCount++
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', ts, position }),
      })
    })

    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async (route) => {
      await new Promise(r => setTimeout(r, 8000))
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: sseBody([{ type: 'catch_up_done' }, { type: 'text', content: 'streaming...' }]),
      }).catch(() => {})
    })

    await setupChat(page)
    await newChat(page)
    await sendMessage(page, 'first message')
    await expect(page.locator('.chat__stop')).toBeVisible({ timeout: 5000 })

    await sendMessage(page, TEXT1)
    await sendMessage(page, TEXT2)
    // Wait for both rows queued + both server-confirmed (steer button shows).
    await page.waitForFunction(
      () => document.querySelectorAll('.queued__row').length === 2,
      { timeout: 5000 },
    )
    const steerBtn = page.getByRole('button', { name: 'Send queued message now' })
    await expect(steerBtn).toBeVisible({ timeout: 5000 })

    await steerBtn.click()
    await expect.poll(
      () => messagePosts.filter(b => b.force_steer).length,
      { timeout: 5000 },
    ).toBe(1)

    const steerPost = messagePosts.find(b => b.force_steer)
    // consume_pending_cids is the two queued rows' cids, in pending order.
    const cid1 = messagePosts.find(b => !b.force_steer && b.content === TEXT1).cid
    const cid2 = messagePosts.find(b => !b.force_steer && b.content === TEXT2).cid
    expect(steerPost.consume_pending_cids).toEqual([cid1, cid2])
    expect(steerPost.content).toBe(`${TEXT1}\n\n${TEXT2}`)
  })

  test('a steer preserves the live stream and held scroll position', async ({ page }) => {
    // Regression for bug #1: a force_steer must inject into the LIVE turn,
    // not trigger the fresh-send reset. The old code ran sendMessage's
    // "new turn" reset (setStreamItems([]) + setIsStreaming + reconnect) for
    // a force_steer too — which (a) WIPED the pre-steer assistant text the
    // SSE had already streamed (and which onSteeredIntoTurn's
    // promoteStreamToMessages reads from latestItemsRef to seal as its own
    // message, so it was lost), and (b) flapped the live stream. The fix
    // skips the reset when forceSteer is set. This test pins both:
    //   - the pre-steer assistant text stays on screen across the steer,
    //   - no second /stream connection is opened (no reconnect flash),
    //   - after the steered_into_turn SSE event the steered user row renders
    //     inline and the pre-steer text is sealed as an assistant message.
    const QUEUE_TS = Date.now() + 60_000
    const QUEUED_TEXT = 'steer me in'
    const PRE_STEER = 'thinking out loud before the steer'
    const POST_STEER = 'continuing after the steered message'

    const messagePosts = []
    let streamConnections = 0

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async (route) => {
      const req = route.request()
      let body = {}
      try { body = JSON.parse(req.postData() || '{}') } catch { /* empty */ }
      messagePosts.push(body)
      if (body.force_steer) {
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'steered', chat_id: 'mock', pending_messages: [],
          }),
        })
      }
      if (body.content === 'first message') {
        return route.fulfill({ status: 202, contentType: 'application/json', body: '{}' })
      }
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'queued', ts: QUEUE_TS, position: 1 }),
      })
    })

    // One held-open SSE for the whole turn. Playwright's route.fulfill()
    // cannot stream incrementally (its body is a string/Buffer), so using it
    // here closes /stream after the pre-steer text and legitimately lets the
    // app reconnect. Mock fetch in the page with a ReadableStream instead:
    // pre-steer text renders immediately, the connection stays open, and the
    // steer boundary is released only after the force_steer POST is observed.
    await page.addInitScript(({ preSteer, postSteer, queuedText, queueTs }) => {
      const originalFetch = window.fetch.bind(window)
      const encoder = new TextEncoder()
      const encodeSse = events => encoder.encode(
        events.map(e => `data: ${JSON.stringify(e)}\n\n`).join(''),
      )
      window.__steerQueuedStreamMock = {
        connections: 0,
        emitSteer: null,
      }
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input?.url
        if (typeof url === 'string' && /\/api\/chats\/[0-9a-f-]+\/stream$/.test(url)) {
          window.__steerQueuedStreamMock.connections += 1
          let emitSteer
          const body = new ReadableStream({
            start(controller) {
              controller.enqueue(encodeSse([
                { type: 'catch_up_done' },
                { type: 'text', content: preSteer },
              ]))
              emitSteer = (steeredMessages = []) => {
                const messages = Array.isArray(steeredMessages) && steeredMessages.length > 0
                  ? steeredMessages
                  : [{ role: 'user', ts: queueTs, cid: `legacy-${queueTs}`, content: queuedText }]
                controller.enqueue(encodeSse([
                  {
                    type: 'steered_into_turn',
                    ts: queueTs,
                    content: queuedText,
                    messages,
                  },
                  { type: 'text', content: postSteer },
                ]))
              }
            },
            cancel() {},
          })
          window.__steerQueuedStreamMock.emitSteer = messages => emitSteer?.(messages)
          return new Response(body, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
          })
        }
        return originalFetch(input, init)
      }
    }, { preSteer: PRE_STEER, postSteer: POST_STEER, queuedText: QUEUED_TEXT, queueTs: QUEUE_TS })

    await setupChat(page)
    await newChat(page)

    await sendMessage(page, 'first message')
    await expect(page.locator('.chat__stop')).toBeVisible({ timeout: 5000 })
    // The pre-steer assistant text streams in.
    await page.waitForFunction(
      (t) => document.body.textContent?.includes(t),
      PRE_STEER, { timeout: 5000 },
    )

    // Queue + steer.
    await sendMessage(page, QUEUED_TEXT)
    const steerBtn = page.getByRole('button', { name: 'Send queued message now' })
    await expect(steerBtn).toBeVisible({ timeout: 5000 })
    const heldBeforeSteer = await page.evaluate(() => {
      const scroll = document.querySelector('.chat__scroll')
      const firstUser = document.querySelector('.chat__msg--user')
      if (!scroll || !firstUser) return null
      return {
        scrollTop: scroll.scrollTop,
        clientHeight: scroll.clientHeight,
        anchorKey: firstUser.dataset.key,
        firstUserVisualTop: firstUser.getBoundingClientRect().top
          - scroll.getBoundingClientRect().top,
      }
    })
    expect(heldBeforeSteer).not.toBeNull()
    await steerBtn.click()
    await expect.poll(
      () => messagePosts.filter(b => b.force_steer).length, { timeout: 5000 },
    ).toBe(1)
    const steerPost = messagePosts.find(b => b.force_steer)
    const steeredMessages = [{
      role: 'user',
      ts: QUEUE_TS,
      cid: steerPost?.consume_pending_cids?.[0],
      content: QUEUED_TEXT,
    }]
    await page.evaluate(
      messages => window.__steerQueuedStreamMock?.emitSteer?.(messages),
      steeredMessages,
    )

    // CORE ASSERTION: the pre-steer text is STILL on screen right after the
    // steer resolved. Before the fix, sendMessage's fresh-send reset cleared
    // streamItems on the force_steer POST and the text vanished. Poll in the
    // BROWSER context (page.evaluate), not Node.
    await expect.poll(
      () => page.evaluate(t => document.body.textContent?.includes(t), PRE_STEER),
      { timeout: 2000 },
    ).toBe(true)
    await page.waitForFunction(
      ({ queuedText, postSteer }) => {
        const text = document.body.textContent || ''
        return text.includes(queuedText) && text.includes(postSteer)
      },
      { queuedText: QUEUED_TEXT, postSteer: POST_STEER },
      { timeout: 5000 },
    )
    const heldAfterSteer = await page.evaluate((anchorKey) => {
      const scroll = document.querySelector('.chat__scroll')
      const users = document.querySelectorAll('.chat__msg--user')
      if (!scroll || users.length < 2) return null
      const scrollTop = scroll.getBoundingClientRect().top
      const heldUser = Array.from(users).find(el => el.dataset.key === anchorKey)
      const steeredUser = Array.from(users).find(el => el !== heldUser)
      if (!heldUser || !steeredUser) return null
      return {
        scrollTop: scroll.scrollTop,
        clientHeight: scroll.clientHeight,
        anchorVisualTop: heldUser.getBoundingClientRect().top - scrollTop,
        steeredUserVisualTop: steeredUser.getBoundingClientRect().top - scrollTop,
      }
    }, heldBeforeSteer.anchorKey)
    expect(heldAfterSteer).not.toBeNull()
    expect(
      Math.abs(heldAfterSteer.anchorVisualTop - heldBeforeSteer.firstUserVisualTop),
      JSON.stringify({ heldBeforeSteer, heldAfterSteer }),
    )
      .toBeLessThanOrEqual(8)
    expect(heldAfterSteer.steeredUserVisualTop).toBeGreaterThan(10)
    streamConnections = await page.evaluate(
      () => window.__steerQueuedStreamMock?.connections || 0,
    )

    // No reconnect flash: the live stream was never torn down + reopened.
    expect(streamConnections).toBe(1)
  })
})
