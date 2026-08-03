/**
 * Spacer / scroll behavior tests for ChatView.
 *
 * Runs against the deployed app with all API calls intercepted — no agent
 * tokens consumed.  Tests the spacer formula, ResizeObserver, scroll
 * restoration, and chat-switching edge cases.
 *
 * Run:  scripts/playwright-local.sh --allow-local-e2e tests/spacer.spec.mjs
 * Debug: scripts/playwright-local.sh --allow-local-e2e tests/spacer.spec.mjs --headed --debug
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { mockAcceptedMessages } from './_mockAcceptedMessages.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Per-worker cleanup: every chat this worker created during this spec
// file is bulk-deleted after the last test. Keeps the chat list from
// piling up across workers + runs. See tests/_chatTracker.mjs.
attachCleanup()

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Log in and return an authenticated page with API interception. */
async function setup(page, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)

  // Intercept agent-related routes — prevents real agent runs and SSE hangs.
  await mockAcceptedMessages(page)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  // Auth is handled by the global setup (storageState).
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
          || document.querySelector('[data-chat-surface="painted"] .chat__scroll')
          || document.querySelector('[data-chat-surface="painted"] .chat__form')),
    { timeout: 10000 }
  )
}

/** Navigate to a new empty chat. */
async function newChat(page) {
  // Create a worker-tagged chat via the API so cleanupWorkerChats
  // can find and delete it after the spec finishes. Navigate to that exact
  // chat on the next shell mount. Clicking the drawer's New-chat action here
  // races its cached chat list: it can reuse an older empty chat or create an
  // untagged second row, which makes retries stateful and defeats cleanup.
  const chat = await createTaggedChat(page)
  if (!chat?.id) throw new Error('failed to create tagged test chat')
  // The versioned workspace is authoritative over the legacy active-chat
  // compatibility mirror. Use the supported explicit deep link so this helper
  // really navigates to the chat even after a previous test engaged a workspace.
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.locator('[data-chat-surface="painted"] .chat__empty-wrap')).toBeVisible({ timeout: 8000 })
}

/** Type a message and press Enter.  Returns after React has rendered. */
async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  // Wait for the scroll container to appear (empty state -> chat state).
  await expect(page.locator('[data-chat-surface="painted"] .chat__scroll')).toBeVisible({ timeout: 3000 })
  // Two rAFs for React to flush layout effects.
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))
}

/** Click the stop button and wait for sending state to clear. */
async function stopAgent(page) {
  await page.evaluate(() => document.querySelector('[data-chat-surface="painted"] .chat__stop')?.click())
  await page.waitForFunction(
    () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
    { timeout: 3000 }
  )
  // Let React settle.
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))
}

/** Read spacer/scroll measurements from the DOM. */
async function measure(page) {
  return page.evaluate(() => {
    const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
    const list = document.querySelector('[data-chat-surface="painted"] .chat__list')
    const userMsgs = document.querySelectorAll('[data-chat-surface="painted"] .chat__msg--user')
    const lastUser = userMsgs[userMsgs.length - 1]
    if (!scroll) throw new Error('chat scroll element is missing')
    return {
      scrollTop: Math.round(scroll.scrollTop),
      clientH: scroll.clientHeight,
      scrollH: scroll.scrollHeight,
      spacerH: parseInt(spacer?.style.height) || 0,
      listH: list?.offsetHeight || 0,
      msgCount: document.querySelectorAll('[data-chat-surface="painted"] .chat__msg').length,
      toolCount: document.querySelectorAll('[data-chat-surface="painted"] .chat__tool').length,
      lastUserTop: lastUser?.offsetTop ?? null,
      // Visual position of last user message relative to viewport.
      userVisualTop: lastUser ? lastUser.offsetTop - scroll.scrollTop : null,
    }
  })
}

/**
 * Inject fake assistant content into the chat list via safe DOM methods.
 * All content is controlled test data, not user input.
 */
async function injectContent(page, textContent, repeat = 1) {
  await page.evaluate(({ text, n }) => {
    const list = document.querySelector('[data-chat-surface="painted"] .chat__list')
    if (!list) return
    let li = list.querySelector('.chat__msg--assistant:last-child')
    if (!li) {
      li = document.createElement('li')
      li.className = 'chat__msg chat__msg--assistant'
      list.appendChild(li)
    }
    const div = document.createElement('div')
    div.className = 'chat__text chat__text--assistant'
    const p = document.createElement('p')
    p.textContent = text.repeat(n)
    div.appendChild(p)
    li.appendChild(div)
  }, { text: textContent, n: repeat })
  // Wait for ResizeObserver to fire.
  await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
}

/** Inject a fake tool block via safe DOM construction. */
async function injectToolBlock(page) {
  await page.evaluate(() => {
    const list = document.querySelector('[data-chat-surface="painted"] .chat__list')
    if (!list) return
    let li = list.querySelector('.chat__msg--assistant:last-child')
    if (!li) {
      li = document.createElement('li')
      li.className = 'chat__msg chat__msg--assistant'
      list.appendChild(li)
    }
    const tools = document.createElement('div')
    tools.className = 'chat__tools'
    const tool = document.createElement('div')
    tool.className = 'chat__tool chat__tool--done'
    const header = document.createElement('div')
    header.className = 'chat__tool-header'
    const name = document.createElement('span')
    name.className = 'chat__tool-name'
    name.textContent = 'Read: /data/apps/test/index.jsx'
    header.appendChild(name)
    tool.appendChild(header)
    const detail = document.createElement('div')
    detail.className = 'chat__tool-detail'
    detail.textContent = 'const App = () => { return <div>Hello</div> }'
    tool.appendChild(detail)
    tools.appendChild(tool)
    li.appendChild(tools)
  })
  await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
}

/** Simulate a lazy renderer resizing content (e.g., highlight.js). */
async function simulateLazyResize(page, extraHeight) {
  await page.evaluate((h) => {
    const blocks = document.querySelectorAll('[data-chat-surface="painted"] .chat__text--assistant')
    const last = blocks[blocks.length - 1]
    if (last) last.style.paddingBottom = `${h}px`
  }, extraHeight)
  await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
}

/**
 * Setup variant that serves a fake SSE stream instead of 204.
 * The stream delivers events through the real React rendering pipeline:
 * SSE parsing -> useStreamConnection -> setStreamItems -> React render.
 */
async function setupWithSSE(page, events, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)

  await mockAcceptedMessages(page)
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  // Serve the fake SSE stream.  Events are delivered as one burst.
  const sseBody = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
      },
      body: sseBody,
    })
  )

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
          || document.querySelector('[data-chat-surface="painted"] .chat__scroll')
          || document.querySelector('[data-chat-surface="painted"] .chat__form')),
    { timeout: 10000 }
  )
}

// ---------------------------------------------------------------------------
// Invariant checks
// ---------------------------------------------------------------------------

function assertUserMsgAtTop(m, label = '') {
  expect(m.userVisualTop, `user msg at top ${label}`)
    .toBeLessThanOrEqual(10)
  expect(m.userVisualTop, `user msg at top ${label}`)
    .toBeGreaterThanOrEqual(-2)
}

function assertSpacerReasonable(m, label = '') {
  expect(m.spacerH, `spacer < viewport ${label}`)
    .toBeLessThanOrEqual(m.clientH)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

test.describe('Spacer mechanics', () => {
  test('1. First message — spacer reserves space, user msg at top', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Hello, first message')

    const m = await measure(page)
    expect(m.msgCount).toBe(2) // user msg + thinking dots
    expect(m.spacerH).toBeGreaterThan(0)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)
  })

  test('2. Second message at the content tail retargets the spacer and pins', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First message')
    const first = await measure(page)
    await stopAgent(page)
    await sendMessage(page, 'Second message')

    const m = await measure(page)
    expect(m.msgCount).toBeGreaterThanOrEqual(2)
    expect(m.spacerH).toBeGreaterThan(0)
    expect(m.lastUserTop).toBeGreaterThan(first.lastUserTop)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)
  })

  test('3. Repeated messages at the content tail pin deterministically', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First')
    const first = await measure(page)
    await stopAgent(page)
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
    await sendMessage(page, 'Second')
    const second = await measure(page)
    await stopAgent(page)
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
    await sendMessage(page, 'Third')

    const third = await measure(page)
    expect(second.lastUserTop).toBeGreaterThan(first.lastUserTop)
    expect(third.lastUserTop).toBeGreaterThan(second.lastUserTop)
    assertUserMsgAtTop(second, 'second tail send')
    assertUserMsgAtTop(third, 'third tail send')
    expect(third.spacerH).toBeGreaterThan(0)
    assertSpacerReasonable(third)
  })
})

test.describe('Streaming content', () => {
  test('4. Spacer shrinks as content grows', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Test streaming')

    const before = await measure(page)
    expect(before.spacerH).toBeGreaterThan(0)

    await injectContent(page, 'Short response line one. ')
    const after1 = await measure(page)
    expect(after1.spacerH).toBeLessThan(before.spacerH)
    expect(after1.listH).toBeGreaterThan(before.listH)

    await injectContent(page, 'Another line of content. ')
    const after2 = await measure(page)
    expect(after2.spacerH).toBeLessThanOrEqual(after1.spacerH)
  })

  test('5. Spacer reaches 0 when content exceeds viewport', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Test overflow')

    await injectContent(page, 'Long content. ', 100)
    const m = await measure(page)
    expect(m.spacerH).toBe(0)
    expect(m.listH).toBeGreaterThan(m.clientH)
  })

  test('6. Tool blocks — spacer adjusts', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Test tools')

    const before = await measure(page)
    await injectToolBlock(page)
    const after = await measure(page)
    expect(after.spacerH).toBeLessThan(before.spacerH)
    expect(after.listH).toBeGreaterThan(before.listH)
  })

  test('7. Lazy resize — spacer adjusts after delayed render', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Test lazy')

    await injectContent(page, 'Code block placeholder. ')
    const before = await measure(page)

    // Simulate highlight.js expanding the element.
    await simulateLazyResize(page, 100)
    const after = await measure(page)
    expect(after.spacerH).toBeLessThan(before.spacerH)
  })
})

test.describe('Short responses', () => {
  test('8. Short response — spacer stays positive after stop', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'What is 2+2?')

    await injectContent(page, 'The answer is 4. ')
    await stopAgent(page)

    const m = await measure(page)
    expect(m.spacerH).toBeGreaterThan(0)
    assertSpacerReasonable(m)
  })

  test('9. Short response — spacer preserved after reload', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Short test')
    await injectContent(page, 'Brief answer. ')
    await stopAgent(page)

    const beforeSwitch = await measure(page)
    expect(beforeSwitch.spacerH).toBeGreaterThan(0)

    // Seed the chat with persisted messages before reload.
    //
    // setup()'s `/api/chats/:id/messages` mock prevented the agent
    // CLI from running, which also means the user's send was never
    // persisted server-side. Reloading against an empty `chat.messages`
    // makes ChatView's post-mount fetch resolve with messages=[],
    // showEmpty flips true, .chat__scroll unmounts and is replaced by
    // .chat__empty-wrap — which is documented intentional behavior
    // (see ARCHITECTURE.md "Chat scroll + steer contract" R1: the
    // scroll container only mounts after the first user send).
    //
    // To exercise the "returning to a chat that has messages" path
    // honestly, PUT a user+assistant pair directly onto the chat row.
    // ChatView's GET /chats/:id?limit=20 then returns real messages,
    // showEmpty stays false, .chat__scroll mounts, scroll restoration
    // runs, and the spacer is recomputed under measurement.
    // Fence: useNavigation persists activeChatId via useEffect after
    // a render commit. sendMessage's two-rAF wait usually flushes that
    // commit, but make the wait explicit so the race becomes
    // structurally impossible: localStorage MUST agree with the chat
    // ChatView is rendering before we PUT messages onto it.
    await page.waitForFunction(
      () => !!(document.querySelector('[data-chat-surface="painted"] .chat__scroll')
              && localStorage.getItem('moebius_active_chat')),
      { timeout: 3000 },
    )
    await page.evaluate(async () => {
      const token = localStorage.getItem('token')
      const chatId = localStorage.getItem('moebius_active_chat')
      if (!token || !chatId) throw new Error('seed precondition: missing token or activeChatId')
      const res = await fetch(`/api/chats/${chatId}`, {
        method: 'PUT',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          // `blocks: []` is explicit: ChatView tolerates missing blocks
          // today via ?. guards but the contract isn't pinned. Set it
          // here so a future tightening of the contract doesn't
          // silently break the seed.
          messages: [
            { role: 'user', content: 'Short test', ts: Date.now() - 1000, blocks: [] },
            { role: 'assistant', content: 'Brief answer.', ts: Date.now(), blocks: [] },
          ],
        }),
      })
      if (!res.ok) throw new Error(`seed PUT failed: ${res.status}`)
    })

    // Reload to simulate returning to the chat.
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    // ChatView intentionally keeps a restored transcript hidden while its
    // quiet-layout pass sizes the reservation. Waiting only for DOM presence
    // races that pass and can sample the spacer's pre-reveal 0px bootstrap.
    await expect(page.locator('[data-chat-surface="painted"] .chat__scroll')).toBeVisible({ timeout: 15000 })
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))

    const afterReturn = await measure(page)
    // No `if (!afterReturn.error)` guard — when state pollution
    // landed us on the wrong chat the older form silently passed.
    expect(afterReturn.error).toBeUndefined()
    expect(afterReturn.spacerH).toBeGreaterThan(0)
    assertSpacerReasonable(afterReturn)
    // The restored reservation is exact: max scroll equals the latest user
    // row's pin target, so it can reach the top but there is no extra blank.
    expect(Math.abs(
      (afterReturn.scrollH - afterReturn.clientH)
      - Math.max(0, afterReturn.lastUserTop - 4)
    )).toBeLessThanOrEqual(2)
  })

  test('10. New send after short response — old spacer replaced', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First question')
    await injectContent(page, 'Short answer. ')
    await stopAgent(page)

    const withOldSpacer = await measure(page)
    expect(withOldSpacer.spacerH).toBeGreaterThan(0)

    // Let spacer recalculation settle before next send.
    await page.evaluate(() => new Promise(r => setTimeout(r, 300)))

    // The short transcript is still at its real-content tail, so the next
    // send follows the same geometry rule as every other send: retarget the
    // permanent reservation and pin the new user row.
    await sendMessage(page, 'Follow-up question')
    const m = await measure(page)
    assertUserMsgAtTop(m, 'follow-up after short response')
    assertSpacerReasonable(m)
    // The spacer should anchor to the new user message, not the old one.
    // DOM-injected content may not survive React re-render, so the new
    // user message can land at the same offset as the first.
    expect(m.lastUserTop).toBeGreaterThanOrEqual(withOldSpacer.lastUserTop)
  })
})

test.describe('Chat switching (the bug)', () => {
  test('11. Cold return preserves the visible owner\'s exact reading position', async ({ page }) => {
    await setup(page)
    await newChat(page)
    const chatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(chatId).toBeTruthy()

    // Persist enough stable transcript rows to make a middle reading position
    // meaningfully different from both the first row and the recent tail.
    await page.evaluate(async ({ id }) => {
      const token = localStorage.getItem('token')
      const now = Date.now()
      const messages = Array.from({ length: 36 }, (_, index) => ({
        role: index % 2 === 0 ? 'user' : 'assistant',
        content: `Return position row ${index}. ${'Stable browser contract text. '.repeat(12)}`,
        ts: now + index,
        cid: index % 2 === 0 ? `return-position-${index}` : undefined,
        blocks: [],
      }))
      const response = await fetch(`/api/chats/${id}`, {
        method: 'PUT',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ messages }),
      })
      if (!response.ok) throw new Error(`seed PUT failed: ${response.status}`)
    }, { id: chatId })

    await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chatId)}`, {
      waitUntil: 'domcontentloaded',
    })
    await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
      .toBeVisible({ timeout: 15000 })

    // Exercise the reader-owned path rather than assigning scrollTop as test
    // setup. The pointer edge opens the controller's gesture window; the
    // resulting ANCHOR_AT write is the product state that must survive return.
    await page.evaluate(() => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!scroll) throw new Error('scroll surface missing')
      scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      scroll.scrollTop = Math.floor(scroll.scrollHeight / 3)
    })
    await page.waitForFunction((id) => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const saved = JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id]
      return scroll?.dataset.scrollMode === 'ANCHOR_AT'
        && saved?.kind === 'ANCHOR_AT'
        && scroll.scrollTop > scroll.clientHeight
    }, chatId)

    const readPosition = () => page.evaluate((id) => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const saved = JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id]
      const anchor = scroll.querySelector(`[data-key="${CSS.escape(saved.key)}"]`)
      return {
        key: saved.key,
        offset: saved.offset,
        anchorTop: anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top,
      }
    }, chatId)
    const beforeReturn = await readPosition()

    // A cold shell mount may retain physical ChatView owners for both workspace
    // worlds. An initially hidden owner has never painted and must not replace
    // the visible owner's durable coordinate while mounting or unloading.
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
      .toBeVisible({ timeout: 15000 })
    await page.waitForFunction((id) => (
      document.querySelector('[data-chat-surface="painted"]')?.dataset.chatId === id
      && document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        ?.dataset.scrollMode === 'ANCHOR_AT'
    ), chatId)

    const afterReturn = await readPosition()

    expect(afterReturn.key).toBe(beforeReturn.key)
    expect(Math.abs(afterReturn.offset - beforeReturn.offset)).toBeLessThanOrEqual(1)
    // A cold fetch may contain fewer paginated rows above the anchor, so raw
    // scrollTop is deliberately not stable. The semantic coordinate must put
    // the same anchor at the same visible viewport position.
    expect(Math.abs(afterReturn.anchorTop - beforeReturn.anchorTop)).toBeLessThanOrEqual(8)
  })
})

test.describe('Empty state transition', () => {
  test('12. visible viewport reservation initializes on empty->chat transition', async ({ page }) => {
    await setup(page)
    await newChat(page)

    // Verify empty state — no scroll container.
    const hasScroll = await page.evaluate(
      () => !!document.querySelector('[data-chat-surface="painted"] .chat__scroll')
    )
    expect(hasScroll).toBe(false)

    // Send first message — scroll container appears.
    await sendMessage(page, 'First ever message')
    const m = await measure(page)

    // Spacer should use the newly mounted visible viewport height, not 0.
    expect(m.spacerH).toBeGreaterThan(0)
    assertSpacerReasonable(m)
    assertUserMsgAtTop(m)
  })
})

test.describe('SSE streaming (real React path)', () => {
  test('15. Text stream via SSE — spacer shrinks as React renders', async ({ page }) => {
    const events = [
      { type: 'catch_up_done' },
      { type: 'text', content: 'Hello ' },
      { type: 'text', content: 'world. ' },
      { type: 'text', content: 'This is a streamed response. '.repeat(10) },
      { type: 'done' },
    ]
    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'SSE test')

    // Wait for the stream to be processed and promote to happen.
    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    // Should have user msg + promoted assistant msg.
    expect(m.msgCount).toBeGreaterThanOrEqual(2)
    assertSpacerReasonable(m)
  })

  test('16. Tool blocks via SSE — rendered through React', async ({ page }) => {
    const events = [
      { type: 'catch_up_done' },
      { type: 'tool_start', tool: 'Read', input: '/data/apps/test/index.jsx' },
      { type: 'tool_output', content: 'const App = () => <div>Test</div>' },
      { type: 'tool_end' },
      { type: 'text', content: 'I read the file. Here is what I found.' },
      { type: 'done' },
    ]
    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'SSE tool test')

    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    expect(m.msgCount).toBeGreaterThanOrEqual(2)
    assertSpacerReasonable(m)

    // A lone operation is its own collapsed disclosure: no redundant activity
    // summary wrapping one identical child row. It keeps the concrete
    // past-tense label and file glyph; the spinner belongs only to a live tool.
    const tools = page.locator('[data-chat-surface="painted"] .chat__tools').first()
    await expect(tools.locator('.chat__activity')).toHaveCount(0)
    const tool = tools.locator(':scope > .chat__tool')
    await expect(tool.locator('.chat__tool-header')).toHaveAttribute('aria-expanded', 'false')
    await expect(tool.locator('.chat__tool-name')).toHaveText(
      'Read /data/apps/test/index.jsx',
    )
    await expect(tool.locator('[data-tool-kind="files"]')).toHaveCount(1)
    await expect(tool.locator('.chat__tool-spin')).toHaveCount(0)
  })

  test('16b. Near-foot activity taps follow deterministically while live descendants churn', async ({ page }) => {
    const events = [
      { type: 'catch_up_done' },
      // Put the final activity disclosure close to the viewport foot once the
      // reader reaches physical bottom. Multiple steps make its open body tall
      // enough that a stale FOLLOW_BOTTOM replay produces an obvious jump.
      { type: 'text', content: 'Lead-in context. '.repeat(90) },
    ]
    for (let i = 0; i < 8; i++) {
      events.push(
        { type: 'tool_start', tool: 'Read', input: `/tmp/step-${i}.txt` },
        { type: 'tool_output', content: `step ${i} output` },
        { type: 'tool_end' },
      )
    }
    events.push({ type: 'done' })

    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'Near-foot disclosure test')
    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 350)))

    // Enter FOLLOW_BOTTOM through the same input→scroll sequence as a reader,
    // rather than assigning scrollTop as app-owned test setup.
    await page.evaluate(async () => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      s.scrollTop = Math.max(0, s.scrollHeight - s.clientHeight - 100)
      await new Promise(requestAnimationFrame)
      s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      s.scrollTop = s.scrollHeight
      s.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
      await new Promise(r => setTimeout(r, 300))
    })

    const header = page.locator('[data-chat-surface="painted"] .chat__activity-header').last()
    await expect(header).toBeVisible()
    const before = await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const b = [...document.querySelectorAll('[data-chat-surface="painted"] .chat__activity-header')].at(-1)
      const sr = s.getBoundingClientRect()
      return {
        top: b.getBoundingClientRect().top,
        relativeTop: b.getBoundingClientRect().top - sr.top,
        viewport: s.clientHeight,
        gap: s.scrollHeight - s.scrollTop - s.clientHeight,
      }
    })
    expect(before.gap).toBeLessThanOrEqual(4)
    expect(before.relativeTop).toBeGreaterThan(before.viewport * 0.55)
    expect(before.relativeTop).toBeLessThan(before.viewport - 20)

    // Simulate status/output churn inside an open live activity timeline. In
    // FOLLOW_BOTTOM, the scroll controller remains the sole authority: opening
    // follows the taller real-content tail and closing returns to the prior
    // tail. Repeated equal states must land identically despite descendant
    // mutations; that is the autoscroll half of the idempotent-toggle contract.
    await page.evaluate(() => {
      window.__disclosureChurn = setInterval(() => {
        const timeline = [...document.querySelectorAll('[data-chat-surface="painted"] .chat__activity-timeline')].at(-1)
        if (!timeline) return
        const marker = document.createElement('i')
        marker.hidden = true
        timeline.appendChild(marker)
        marker.remove()
      }, 5)
    })

    try {
      let openTop = null
      for (let i = 0; i < 10; i++) {
        await header.click()
        await page.evaluate(() => new Promise(r =>
          requestAnimationFrame(() => requestAnimationFrame(r))))
        const after = await page.evaluate(() => {
          const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
          const b = [...document.querySelectorAll('[data-chat-surface="painted"] .chat__activity-header')].at(-1)
          return {
            top: b.getBoundingClientRect().top,
            gap: s.scrollHeight - s.scrollTop - s.clientHeight,
          }
        })
        expect(after.gap, `toggle ${i + 1} left the tail`).toBeLessThanOrEqual(4)
        if (i % 2 === 0) {
          if (openTop == null) {
            openTop = after.top
            expect(openTop).toBeLessThan(before.top - 20)
          } else {
            expect(Math.abs(after.top - openTop), `open ${i + 1} drift`).toBeLessThanOrEqual(2)
          }
        } else {
          expect(Math.abs(after.top - before.top), `closed ${i + 1} drift`).toBeLessThanOrEqual(2)
        }
      }
    } finally {
      await page.evaluate(() => clearInterval(window.__disclosureChurn))
    }
  })

  test('17. Long SSE response fills the reservation and follows the tail', async ({ page }) => {
    const events = [
      { type: 'catch_up_done' },
      { type: 'text', content: 'Very long response. '.repeat(200) },
      { type: 'done' },
    ]
    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'SSE long test')

    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    // Content should overflow the viewport.
    expect(m.listH).toBeGreaterThan(m.clientH)
    // Once the reply consumes the exact reservation, the live pin performs
    // its single automatic handoff to real-content tail follow.
    const gap = m.scrollH - m.scrollTop - m.clientH
    expect(Math.abs(gap)).toBeLessThanOrEqual(4)
  })
})

test.describe('Autoscroll behavior', () => {
  test('18. Auto-follows when near bottom during streaming', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Autoscroll test')

    // Inject enough content to overflow the viewport.
    await injectContent(page, 'Filling viewport with lots of text. ', 150)
    const before = await measure(page)
    expect(before.listH).toBeGreaterThan(before.clientH)

    // Two-step gesture simulation for the new state-machine design:
    //   1. Programmatic scroll positions content at the bottom (no
    //      mode transition — outside gesture window).
    //   2. Wait for the IntersectionObserver to settle
    //      bottomVisibleRef to true (50ms debounce inside the hook).
    //   3. Dispatch a pointerdown + a tiny scroll within the gesture
    //      window. The scroll handler now sees bottomVisibleRef=true
    //      AND userDriven=true → transitions mode to FOLLOW_BOTTOM.
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (s) s.scrollTop = s.scrollHeight
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!s) return
      s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      // Tiny in-window scroll forces onScroll to re-evaluate mode.
      s.scrollTop = Math.max(0, s.scrollTop - 1)
      s.scrollTop = s.scrollHeight
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    const atBottom = await measure(page)
    const bottomGap = atBottom.scrollH - atBottom.scrollTop - atBottom.clientH
    expect(bottomGap).toBeLessThanOrEqual(5)

    // Inject content in small chunks to simulate streaming, giving the RO
    // time to fire between each.
    for (let i = 0; i < 5; i++) {
      await injectContent(page, `Streaming chunk ${i + 1}. More text here. `, 5)
    }

    const after = await measure(page)
    const afterGap = after.scrollH - after.scrollTop - after.clientH
    // Should still be near the bottom (auto-followed).
    expect(afterGap).toBeLessThan(200)
  })

  test('19. Does NOT auto-follow when user scrolled up', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'No auto-follow test')

    // Fill viewport.
    await injectContent(page, 'Filling up with content. ', 150)

    // Scroll to middle (user deliberately scrolled up).
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!s) return
      s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      s.scrollTop = s.scrollHeight / 2
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    const midScroll = await measure(page)
    const savedTop = midScroll.scrollTop

    // Inject more content.
    await injectContent(page, 'More content arriving. ', 20)

    const after = await measure(page)
    // User's scroll position should NOT have jumped to the bottom.
    // Allow small tolerance for spacer recalc.
    expect(after.scrollTop).toBeLessThan(after.scrollH - after.clientH - 50)
  })

  test('20. SSE streaming follows only after the reservation is consumed', async ({ page }) => {
    // Simulate a long SSE response delivered in chunks.
    const chunks = []
    for (let i = 0; i < 20; i++) {
      chunks.push({ type: 'text', content: `Paragraph ${i + 1}. ${'Content here. '.repeat(10)} ` })
    }
    const events = [
      { type: 'catch_up_done' },
      ...chunks,
      { type: 'done' },
    ]
    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'SSE autoscroll test')

    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    // Content should exceed viewport.
    expect(m.listH).toBeGreaterThan(m.clientH)
    // This response is deliberately long enough to consume the reservation,
    // so the one pin-to-follow handoff has occurred by terminal settlement.
    const gap = m.scrollH - m.scrollTop - m.clientH
    expect(Math.abs(gap)).toBeLessThanOrEqual(4)
  })

  test('20b. User message stays pinned near top through real SSE stream', async ({ page }) => {
    // Replaces removed test 25. Uses real SSE/React path (not DOM
    // injection) to verify the user message remains near the top
    // after a long streaming response completes and is promoted.
    const chunks = []
    for (let i = 0; i < 15; i++) {
      chunks.push({ type: 'text', content: `Line ${i + 1}. ${'Filling. '.repeat(8)} ` })
    }
    await setupWithSSE(page, [
      { type: 'catch_up_done' },
      ...chunks,
      { type: 'done' },
    ])
    await newChat(page)
    await sendMessage(page, 'Pin test')

    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    assertUserMsgAtTop(m, 'after stream completion')
  })
})

test.describe('Viewport sizes', () => {
  test('13. Desktop viewport', async ({ page }) => {
    await setup(page, { width: 1280, height: 800 })
    await newChat(page)
    await sendMessage(page, 'Desktop test')

    const m = await measure(page)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)

    await injectContent(page, 'Desktop content. ', 200)
    const after = await measure(page)
    expect(after.spacerH).toBe(0)
  })

  test('14. Mobile viewport', async ({ page }) => {
    await setup(page, { width: 412, height: 915 })
    await newChat(page)
    await sendMessage(page, 'Mobile test')

    const m = await measure(page)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)

    await injectContent(page, 'Mobile content. ', 80)
    const after = await measure(page)
    expect(after.spacerH).toBe(0)
  })
})

test.describe('Scroll edge cases', () => {
  test('21. Scroll preserved after stream end when user scrolled up', async ({ page }) => {
    // Long SSE response overflowing the viewport.
    const chunks = []
    for (let i = 0; i < 30; i++) {
      chunks.push({ type: 'text', content: `Paragraph ${i}. ${'Text here. '.repeat(12)} ` })
    }
    const events = [{ type: 'catch_up_done' }, ...chunks, { type: 'done' }]
    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'Scroll preservation test')

    // Wait for stream to complete.
    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    // Verify content overflows.
    const before = await measure(page)
    expect(before.listH).toBeGreaterThan(before.clientH + 100)

    // Scroll up to ~1/3.
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (s) s.scrollTop = Math.max(0, s.scrollHeight / 3)
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 200)))

    const scrollBefore = await page.evaluate(() =>
      document.querySelector('[data-chat-surface="painted"] .chat__scroll')?.scrollTop ?? 0
    )
    expect(scrollBefore).toBeGreaterThan(0)

    // Wait and verify no stale auto-follow snaps back.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1000)))

    const scrollAfter = await page.evaluate(() =>
      document.querySelector('[data-chat-surface="painted"] .chat__scroll')?.scrollTop ?? 0
    )
    expect(Math.abs(scrollAfter - scrollBefore)).toBeLessThan(5)
  })

  test('22. Auto-follow survives content bursts during streaming', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Burst test')

    // Start at the bottom (auto-follow engaged).
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (s) s.scrollTop = s.scrollHeight
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    // Inject a large burst of content (simulates a big code block rendering).
    await injectContent(page, 'Large code block line. ', 50)

    const m = await measure(page)
    const gap = m.scrollH - m.scrollTop - m.clientH
    // Should still be near the bottom — auto-follow survived the burst.
    expect(gap).toBeLessThan(50)
  })

  test('23. User scroll-up disengages auto-follow mid-stream', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Disengage test')

    // Fill viewport.
    await injectContent(page, 'Initial content. ', 100)

    // Start at the bottom.
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (s) s.scrollTop = s.scrollHeight
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    // Scroll up past 50px threshold.
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!s) return
      // This test claims reader ownership, so exercise the actual contract:
      // input opens the gesture window before the browser scroll lands.
      // A bare scrollTop assignment is app-owned test setup and must not
      // change ScrollMode.
      s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      s.scrollTop = Math.max(0, s.scrollTop - 200)
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    const scrollBefore = await page.evaluate(() =>
      document.querySelector('[data-chat-surface="painted"] .chat__scroll')?.scrollTop ?? 0
    )

    // Inject more content — should NOT auto-follow.
    await injectContent(page, 'New content arriving. ', 20)

    const scrollAfter = await page.evaluate(() =>
      document.querySelector('[data-chat-surface="painted"] .chat__scroll')?.scrollTop ?? 0
    )

    // Position should not have jumped to the bottom.
    const m = await measure(page)
    const gapFromBottom = m.scrollH - scrollAfter - m.clientH
    expect(gapFromBottom).toBeGreaterThan(50)
  })

  test('25. Reserved-tail swipe follows tool output without jumping backward', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Autoscroll tight test')

    const before = await measure(page)
    expect(before.spacerH).toBeGreaterThan(0)
    expect(before.userVisualTop).toBeGreaterThanOrEqual(0)
    expect(before.userVisualTop).toBeLessThanOrEqual(10)

    // The fresh pin is already clamped at the physical bottom, so the browser
    // cannot emit another scroll event. A deliberate swipe toward the end is
    // nevertheless explicit follow intent.
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!s) return
      s.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, pointerType: 'touch', clientY: 400,
      }))
      s.dispatchEvent(new PointerEvent('pointermove', {
        bubbles: true, pointerType: 'touch', clientY: 360,
      }))
      s.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true, pointerType: 'touch', clientY: 360,
      }))
    })
    await page.waitForFunction(() => (
      document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        ?.dataset.scrollMode === 'FOLLOW_BOTTOM'
    ))

    const atBottom = await measure(page)
    expect(atBottom.scrollH - atBottom.scrollTop - atBottom.clientH)
      .toBeLessThanOrEqual(5)

    // Tool calls consume reserved room first. Follow stays at the same physical
    // tail rather than jumping backward to a second "real content" bottom.
    for (let i = 0; i < 5; i++) {
      await injectToolBlock(page)
    }

    const after = await measure(page)
    expect(after.toolCount).toBe(5)
    const afterGap = after.scrollH - after.scrollTop - after.clientH
    expect(afterGap).toBeLessThanOrEqual(10)
  })

  test('28. Keyboard-sized viewport consumes blank reservation before lifting output', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Responsive keyboard spacer test')

    // The fresh pin is already at the physical tail. This upward swipe is the
    // explicit reader action that asks the chat to keep following it.
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!s) return
      s.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, pointerType: 'touch', clientY: 400,
      }))
      s.dispatchEvent(new PointerEvent('pointermove', {
        bubbles: true, pointerType: 'touch', clientY: 360,
      }))
      s.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true, pointerType: 'touch', clientY: 360,
      }))
    })
    await page.waitForFunction(() => (
      document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        ?.dataset.scrollMode === 'FOLLOW_BOTTOM'
    ))

    const closed = await measure(page)
    expect(closed.spacerH).toBeGreaterThan(300)

    // Simulate the shell's final keyboard-open geometry. While reservation is
    // still available, the smaller box must remove blank spacer one-for-one so
    // the prompt and physical tail remain at the same scroll coordinate.
    await page.setViewportSize({ width: 412, height: 615 })
    await page.waitForFunction(({ oldClientH, oldSpacerH }) => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
      const spacerH = parseInt(spacer?.style.height) || 0
      return s?.dataset.scrollMode === 'FOLLOW_BOTTOM'
        && s.clientHeight < oldClientH
        && spacerH < oldSpacerH
    }, { oldClientH: closed.clientH, oldSpacerH: closed.spacerH })

    const open = await measure(page)
    expect(Math.abs(open.scrollTop - closed.scrollTop)).toBeLessThanOrEqual(8)
    expect(Math.abs(open.userVisualTop - closed.userVisualTop)).toBeLessThanOrEqual(8)
    expect(open.scrollH - open.scrollTop - open.clientH).toBeLessThanOrEqual(8)
    expect(Math.abs(
      (closed.spacerH - open.spacerH) - (closed.clientH - open.clientH),
    )).toBeLessThanOrEqual(8)

    // Once output exceeds the smaller visible screen, no blank room remains;
    // FOLLOW_BOTTOM then lifts only the overflow and continues at the tail.
    await injectContent(page, 'Keyboard-visible streamed output. ', 120)
    const overflow = await measure(page)
    expect(overflow.spacerH).toBe(0)
    expect(overflow.scrollTop).toBeGreaterThan(open.scrollTop + 20)
    expect(overflow.scrollH - overflow.scrollTop - overflow.clientH)
      .toBeLessThanOrEqual(10)
  })

  test('24. Auto-follow re-engages when user scrolls back to bottom', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Re-engage test')

    // Helper: simulate a user-driven scroll. pointerdown opens the
    // gesture window; the scrollTop write within it is treated as
    // user intent and may transition the ScrollMode.
    const userScrollTo = async (top) => {
      if (top === 'bottom') {
        // Move away in a separate frame first. Chromium can coalesce an
        // already-clamped bottom write into no scroll event, which would test
        // a no-op gesture rather than the FOLLOW_BOTTOM transition.
        await page.evaluate(() => {
          const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
          if (s) s.scrollTop = Math.max(0, s.scrollHeight - s.clientHeight - 80)
        })
        await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))
      }
      await page.evaluate((t) => {
        const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        if (!s) return
        s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
        s.scrollTop = t === 'bottom' ? s.scrollHeight
          : t === 'up' ? Math.max(0, s.scrollTop - 200)
          : t
      }, top)
      const expectedMode = top === 'bottom' ? 'FOLLOW_BOTTOM' : 'ANCHOR_AT'
      await page.waitForFunction(kind => {
        const id = localStorage.getItem('moebius_active_chat')
        const modes = JSON.parse(localStorage.getItem('chat-reading-position') || '{}')
        const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        return !!id
          && scroll?.dataset.scrollMode === kind
          && modes[id]?.kind === kind
      }, expectedMode, { timeout: 3000 })
    }

    // Fill viewport and engage auto-follow at the bottom (user gesture).
    await injectContent(page, 'Initial content. ', 100)
    await userScrollTo('bottom')

    // Scroll up — disengages auto-follow.
    await userScrollTo('up')

    // Inject content — should NOT follow (user scrolled up).
    await injectContent(page, 'While scrolled up. ', 10)
    const midGap = await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return s ? s.scrollHeight - s.scrollTop - s.clientHeight : 0
    })
    expect(midGap).toBeGreaterThan(50)

    // Now scroll back to bottom — should re-engage auto-follow.
    await userScrollTo('bottom')

    // Inject more content — should auto-follow again.
    await injectContent(page, 'After re-engage. ', 10)
    // FOLLOW_BOTTOM is persisted by the scroll event before the controller's
    // finite reader-ownership window expires. Wait for the deferred layout pass
    // it schedules, rather than sampling the intentional intermediate frame.
    await page.waitForFunction(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return !!s && s.scrollHeight - s.scrollTop - s.clientHeight < 50
    }, undefined, { timeout: 3000 })
    const afterGap = await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return s ? s.scrollHeight - s.scrollTop - s.clientHeight : 0
    })
    expect(afterGap).toBeLessThan(50)
  })

  test('26. Fresh second send while scrolled up preserves the reading anchor', async ({ page }) => {
    // A subsequent send outside gesture-entered auto-scroll reserves room for
    // its reply but leaves the reader at the exact current position.
    //
    // Uses the REAL SSE path (not injectContent) so the long first
    // response PERSISTS across the second send — DOM-injected content
    // evaporates on React's re-render, collapsing the chat to a
    // fits-the-viewport state that (correctly) pins, which can't
    // represent a scrolled-up-in-overflow send. The scroll-position
    // preservation invariant is asserted in send-rule.spec.mjs; here the
    // spacer-spec lock-in is "no pin when scrolled up".
    const events = [
      { type: 'catch_up_done' },
      { type: 'text', content: 'First response paragraph. '.repeat(120) },
      { type: 'done' },
    ]
    await setupWithSSE(page, events)
    await newChat(page)

    await sendMessage(page, 'First message')
    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 300)))

    // Overflowing content confirmed, then scroll up to read via a real
    // gesture (pointerdown + scroll inside the gesture window) → ANCHOR_AT.
    const overflow = await measure(page)
    expect(overflow.scrollH).toBeGreaterThan(overflow.clientH)
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!s) return
      s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      s.scrollTop = Math.max(0, Math.floor(s.scrollHeight / 3))
    })
    // Close the 250ms gesture window so ANCHOR_AT is the settled mode.
    await page.evaluate(() => new Promise(r => setTimeout(r, 350)))

    const before = await measure(page)
    const savedTop = before.scrollTop
    expect(savedTop).toBeGreaterThan(20)
    const gapBefore = before.scrollH - before.scrollTop - before.clientH
    expect(gapBefore).toBeGreaterThan(50)

    // Send the second message while scrolled up.
    await sendMessage(page, 'Second message')
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 120)))
    ))

    const userMsgs = await page.evaluate(() => {
      const msgs = document.querySelectorAll('[data-chat-surface="painted"] .chat__text--user')
      return [...msgs].map(el => el.textContent.trim())
    })
    expect(userMsgs).toContain('Second message')

    // CRITICAL: no pin while the reader is holding a reading anchor.
    const after = await measure(page)
    expect(Math.abs(after.scrollTop - savedTop)).toBeLessThanOrEqual(8)
    expect(after.spacerH).toBeGreaterThanOrEqual(0)
  })

  test('27. Viewport cycles preserve a sent-message pin and an older anchor', async ({ page }) => {
    // The actual chat scroll box is the single keyboard/layout signal. Repeated
    // resize cycles reapply whichever semantic mode already owns the chat;
    // geometry must never replace a pin or exact anchor with another alignment.
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First')

    const pinned = await measure(page)
    expect(pinned.userVisualTop).toBeGreaterThanOrEqual(0)
    expect(pinned.userVisualTop).toBeLessThanOrEqual(10)
    for (let i = 0; i < 3; i++) {
      for (const height of [615, 915]) {
        await page.setViewportSize({ width: 412, height })
        await page.evaluate(() => new Promise(r => setTimeout(r, 100)))
        const duringCycle = await measure(page)
        expect(Math.abs(duringCycle.userVisualTop - pinned.userVisualTop))
          .toBeLessThanOrEqual(8)
        await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
          .toHaveAttribute('data-scroll-mode', 'PIN_USER_MSG')
      }
    }

    // Grow the content so there's room to scroll up.
    await injectContent(page, 'Padding content line. ', 80)
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))

    // Scroll to the top through the reader-owned path (definitely not near the
    // bottom). A bare scrollTop write is browser/programmatic layout, not
    // reading intent, and deliberately cannot replace the keyboard snapshot.
    await page.evaluate(() => {
      const s = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!s) return
      s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      s.scrollTop = 0
      s.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 350)))

    const before = await measure(page)

    for (let i = 0; i < 5; i++) {
      await page.setViewportSize({ width: 412, height: 615 })
      await page.evaluate(() => new Promise(r => setTimeout(r, 80)))
      await page.setViewportSize({ width: 412, height: 915 })
      await page.evaluate(() => new Promise(r => setTimeout(r, 80)))
    }

    const after = await measure(page)
    expect(Math.abs(after.scrollTop - before.scrollTop)).toBeLessThan(20)
  })

  test('28. An already-focused paste follows the tail before the composer grows', async ({ page }) => {
    const events = [
      { type: 'catch_up_done' },
      { type: 'text', content: 'Long response paragraph. '.repeat(120) },
      { type: 'done' },
    ]
    await setupWithSSE(page, events, { width: 412, height: 615 })
    await newChat(page)
    await sendMessage(page, 'First message')
    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      { timeout: 10000 }
    )

    const overflow = await measure(page)
    expect(overflow.scrollH).toBeGreaterThan(overflow.clientH + 100)

    // Establish an ordinary reading anchor, then put that held viewport at the
    // physical tail without manufacturing FOLLOW_BOTTOM. This is the restored
    // / already-focused state that previously depended on a fresh composer tap.
    await page.evaluate(() => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      scroll.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        button: 0,
        pointerType: 'touch',
      }))
      scroll.scrollTop = Math.max(0, Math.floor(scroll.scrollHeight / 3))
      scroll.dispatchEvent(new Event('scroll', { bubbles: true }))
      scroll.dispatchEvent(new PointerEvent('pointerup', {
        bubbles: true,
        button: 0,
        pointerType: 'touch',
      }))
    })
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 350)))
    await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
      .toHaveAttribute('data-scroll-mode', 'ANCHOR_AT')
    await page.evaluate(() => {
      const surface = document.querySelector('[data-chat-surface="painted"]')
      const scroll = surface.querySelector('.chat__scroll')
      const input = surface.querySelector('.chat__input')
      scroll.scrollTop = scroll.scrollHeight
      input.focus({ preventScroll: true })
    })
    const atTail = await measure(page)
    expect(atTail.scrollH - atTail.scrollTop - atTail.clientH).toBeLessThanOrEqual(4)
    await expect(page.locator('[data-chat-surface="painted"] .chat__scroll'))
      .toHaveAttribute('data-scroll-mode', 'ANCHOR_AT')

    const result = await page.evaluate(async () => {
      const surface = document.querySelector('[data-chat-surface="painted"]')
      const scroll = surface.querySelector('.chat__scroll')
      const input = surface.querySelector('.chat__input')
      const foot = surface.querySelector('.chat__foot')
      window.__mobiusChatScrollTrace = {
        version: 1,
        transitions: [],
        writes: [],
        events: [],
      }
      const sample = () => ({
        mode: scroll.dataset.scrollMode,
        top: Math.round(scroll.scrollTop),
        height: scroll.scrollHeight,
        viewport: scroll.clientHeight,
        gap: Math.round(scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight),
        inputHeight: input.offsetHeight,
        footHeight: foot.offsetHeight,
      })

      // A bottom-edge touch can still be waiting to prove whether a native
      // scroll will arrive. Paste from the software keyboard then changes the
      // already-focused textarea without another DOM pointer event.
      scroll.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true,
        button: 0,
        pointerType: 'touch',
      }))
      const before = sample()
      const lines = [
        'First pasted line that wraps across the narrow composer width.',
        'Second pasted line that also grows the composer.',
        'Third pasted line.',
        'Fourth pasted line.',
      ]
      const valueSetter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        'value',
      ).set
      valueSetter.call(input, lines[0])
      input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'insertFromPaste',
        data: lines[0],
      }))
      const immediate = sample()
      const settledFrames = []
      for (const line of lines.slice(1)) {
        const next = `${input.value}\n${line}`
        valueSetter.call(input, next)
        input.dispatchEvent(new InputEvent('input', {
          bubbles: true,
          inputType: 'insertLineBreak',
          data: line,
        }))
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
        settledFrames.push(sample())
      }
      await new Promise(resolve => setTimeout(resolve, 2100))
      const afterFormerGestureCap = sample()
      const composerEvents = window.__mobiusChatScrollTrace.events
        .filter(event => event.event === 'reader:composer-bottom')
      return {
        before,
        immediate,
        settledFrames,
        afterFormerGestureCap,
        composerEvents,
      }
    })

    expect(result.immediate.mode).toBe('FOLLOW_BOTTOM')
    expect(result.composerEvents).toHaveLength(1)
    expect(result.settledFrames.at(-1).footHeight).toBeGreaterThan(result.before.footHeight)
    for (const frame of result.settledFrames) {
      expect(frame.gap).toBeLessThanOrEqual(4)
    }
    expect(result.afterFormerGestureCap.gap).toBeLessThanOrEqual(4)
    expect(Math.abs(
      result.afterFormerGestureCap.top - result.settledFrames.at(-1).top
    )).toBeLessThanOrEqual(4)
  })
})
