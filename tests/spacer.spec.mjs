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
    undefined, { timeout: 10000 }
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

/** Wait for the terminal assistant row, not merely an already-absent Stop. */
async function waitForSettledAssistant(page) {
  const surface = page.locator('[data-chat-surface="painted"]')
  await expect(surface.locator('.chat__msg--assistant').last())
    .toBeVisible({ timeout: 10000 })
  await expect(surface.locator('.chat__stop')).toHaveCount(0, { timeout: 10000 })
  await page.evaluate(() => new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))
  ))
}

/** Click the stop button and wait for sending state to clear. */
async function stopAgent(page) {
  await page.evaluate(() => document.querySelector('[data-chat-surface="painted"] .chat__stop')?.click())
  await page.waitForFunction(
    () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
    undefined, { timeout: 3000 }
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

  const acceptedMessages = await mockAcceptedMessages(page)
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  // Serve the fake SSE stream.  Events are delivered as one burst.
  let assistantSequence = 0
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
    const chatId = new URL(route.request().url()).pathname.split('/').at(-2)
    const assistantId = `e2e-assistant-${++assistantSequence}`
    const identifiedEvents = events.map(event => ({
      ...event,
      assistant_message_id: assistantId,
    }))
    const blocks = []
    for (const event of identifiedEvents) {
      if (event.type === 'text') {
        const previous = blocks.at(-1)
        if (previous?.type === 'text') previous.content += event.content || ''
        else blocks.push({ type: 'text', content: event.content || '' })
      } else if (event.type === 'tool_start') {
        blocks.push({
          type: 'tool',
          tool: event.tool,
          input: event.input || '',
          output: '',
          status: 'running',
        })
      } else if (event.type === 'tool_output') {
        const tool = blocks.findLast(block => block.type === 'tool')
        if (tool) tool.output += event.content || ''
      } else if (event.type === 'tool_end') {
        const tool = blocks.findLast(block => block.type === 'tool')
        if (tool) tool.status = 'done'
      }
    }
    const settlement = acceptedMessages.beginAssistantSettlement(chatId)
    const sseBody = identifiedEvents
      .map(event => `data: ${JSON.stringify(event)}\n\n`)
      .join('')
    await route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
      },
      body: sseBody,
    })
    settlement.complete({
      id: assistantId,
      role: 'assistant',
      content: blocks
        .filter(block => block.type === 'text')
        .map(block => block.content)
        .join('\n\n'),
      blocks,
      ts: Date.now(),
    })
  })

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
          || document.querySelector('[data-chat-surface="painted"] .chat__scroll')
          || document.querySelector('[data-chat-surface="painted"] .chat__form')),
    undefined, { timeout: 10000 }
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

  test('2. Second message at the physical tail retargets the spacer and pins', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First message')
    const first = await measure(page)
    await stopAgent(page)
    const scroll = page.locator('[data-chat-surface="painted"] .chat__scroll')
    const composer = page.getByRole('textbox', { name: 'Message Möbius…' })
    await expect(scroll).toHaveAttribute('data-scroll-mode', 'PIN_USER_MSG')
    await composer.fill('Second message')
    await expect(composer).toHaveValue('Second message')
    await expect(scroll).toHaveAttribute('data-scroll-mode', 'FOLLOW_BOTTOM')
    await composer.press('Enter')
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))

    const m = await measure(page)
    expect(m.msgCount).toBeGreaterThanOrEqual(2)
    expect(m.spacerH).toBeGreaterThan(0)
    expect(m.lastUserTop).toBeGreaterThan(first.lastUserTop)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)
  })


})

test.describe('Streaming content', () => {






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




})

test.describe('Chat switching (the bug)', () => {

})

test.describe('Empty state transition', () => {

})

test.describe('SSE streaming (real React path)', () => {






  test('17. Long SSE response fills the reservation and follows the tail', async ({ page }) => {
    const events = [
      { type: 'catch_up_done' },
      { type: 'text', content: 'Very long response. '.repeat(200) },
      { type: 'done' },
    ]
    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'SSE long test')

    await waitForSettledAssistant(page)

    const m = await measure(page)
    // Content should overflow the viewport.
    expect(m.listH).toBeGreaterThan(m.clientH)
    // Once the reply consumes the exact reservation, the live pin performs
    // its single automatic handoff to real-content tail follow.
    const gap = m.scrollH - m.scrollTop - m.clientH
    expect(Math.abs(gap)).toBeLessThanOrEqual(4)
    // The terminal cursor is absolutely positioned while live and its removal
    // must not leave a layout artifact or move the followed surface.
    await expect(page.locator('[data-chat-surface="painted"] .chat__cursor')).toHaveCount(0)
  })
})

test.describe('Autoscroll behavior', () => {


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




})

test.describe('Viewport sizes', () => {



})

test.describe('Scroll edge cases', () => {




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


})
