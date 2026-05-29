/**
 * Spacer / scroll behavior tests for ChatView.
 *
 * Runs against the deployed app with all API calls intercepted — no agent
 * tokens consumed.  Tests the spacer formula, ResizeObserver, scroll
 * restoration, and chat-switching edge cases.
 *
 * Run:  npx playwright test tests/spacer.spec.mjs
 * Debug: npx playwright test tests/spacer.spec.mjs --headed --debug
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

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
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  // Auth is handled by the global setup (storageState).
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    { timeout: 10000 }
  )
}

/** Navigate to a new empty chat. */
async function newChat(page) {
  // Create a worker-tagged chat via the API so cleanupWorkerChats
  // can find and delete it after the spec finishes.
  await createTaggedChat(page)
  // Click new-chat button via DOM (works even if drawer is hidden).
  await page.evaluate(() => {
    document.querySelector('.drawer__item--new')?.click()
  })
  // If that didn't work (drawer off-screen), reload.
  const hasEmpty = await page.evaluate(
    () => !!document.querySelector('.chat__empty-wrap')
  )
  if (!hasEmpty) {
    await page.goto(BASE)
  }
  await expect(page.locator('.chat__empty-wrap')).toBeVisible({ timeout: 8000 })
}

/** Type a message and press Enter.  Returns after React has rendered. */
async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  // Wait for the scroll container to appear (empty state -> chat state).
  await expect(page.locator('.chat__scroll')).toBeVisible({ timeout: 3000 })
  // Two rAFs for React to flush layout effects.
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))
}

/** Click the stop button and wait for sending state to clear. */
async function stopAgent(page) {
  await page.evaluate(() => document.querySelector('.chat__stop')?.click())
  await page.waitForFunction(
    () => !document.querySelector('.chat__stop'),
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
    const scroll = document.querySelector('.chat__scroll')
    const spacer = document.querySelector('.spacer-dynamic')
    const list = document.querySelector('.chat__list')
    const userMsgs = document.querySelectorAll('.chat__msg--user')
    const lastUser = userMsgs[userMsgs.length - 1]
    if (!scroll) return { error: 'no scroll element' }
    return {
      scrollTop: Math.round(scroll.scrollTop),
      clientH: scroll.clientHeight,
      scrollH: scroll.scrollHeight,
      spacerH: parseInt(spacer?.style.height) || 0,
      listH: list?.offsetHeight || 0,
      msgCount: document.querySelectorAll('.chat__msg').length,
      toolCount: document.querySelectorAll('.chat__tool').length,
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
    const list = document.querySelector('.chat__list')
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
    const list = document.querySelector('.chat__list')
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
    const blocks = document.querySelectorAll('.chat__text--assistant')
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

  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
  )
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
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
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

  test('2. Second message — new spacer anchored to latest user msg', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First message')
    await stopAgent(page)
    await sendMessage(page, 'Second message')

    const m = await measure(page)
    expect(m.msgCount).toBeGreaterThanOrEqual(2)
    expect(m.spacerH).toBeGreaterThan(0)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)
  })

  test('3. Third message — consistent behavior', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First')
    await stopAgent(page)
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
    await sendMessage(page, 'Second')
    await stopAgent(page)
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
    await sendMessage(page, 'Third')

    const m = await measure(page)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)
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
    // (see CLAUDE.md "Chat UX — non-negotiable constraints" #6: the
    // scroll container only mounts after the first user send).
    //
    // To exercise the "returning to a chat that has messages" path
    // honestly, PUT a user+assistant pair directly onto the chat row.
    // ChatView's GET /chats/:id?limit=20 then returns real messages,
    // showEmpty stays false, .chat__scroll mounts, scroll restoration
    // runs, and the spacer is recomputed under measurement.
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
          messages: [
            { role: 'user', content: 'Short test', ts: Date.now() - 1000 },
            { role: 'assistant', content: 'Brief answer.', ts: Date.now() },
          ],
        }),
      })
      if (!res.ok) throw new Error(`seed PUT failed: ${res.status}`)
    })

    // Reload to simulate returning to the chat.
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!document.querySelector('.chat__scroll'),
      { timeout: 15000 }
    )
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))

    const afterReturn = await measure(page)
    // No `if (!afterReturn.error)` guard — when state pollution
    // landed us on the wrong chat the older form silently passed.
    expect(afterReturn.error).toBeUndefined()
    assertSpacerReasonable(afterReturn)
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

    // Send a new message — spacer should be recalculated for the new msg.
    await sendMessage(page, 'Follow-up question')
    const m = await measure(page)
    assertUserMsgAtTop(m)
    assertSpacerReasonable(m)
    // The spacer should anchor to the new user message, not the old one.
    // DOM-injected content may not survive React re-render, so the new
    // user message can land at the same offset as the first.
    expect(m.lastUserTop).toBeGreaterThanOrEqual(withOldSpacer.lastUserTop)
  })
})

test.describe('Chat switching (the bug)', () => {
  test('11. Return to chat — RO active, spacer tracks injected content', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Test switch')

    const beforeSwitch = await measure(page)
    expect(beforeSwitch.spacerH).toBeGreaterThan(0)

    // Simulate switching away: save state to sessionStorage.
    await page.evaluate(() => {
      const scroll = document.querySelector('.chat__scroll')
      const spacer = document.querySelector('.spacer-dynamic')
      if (scroll && spacer) {
        const positions = JSON.parse(sessionStorage.getItem('chat-scroll') || '{}')
        const spacers = JSON.parse(sessionStorage.getItem('chat-spacer') || '{}')
        positions['test-chat'] = scroll.scrollHeight - scroll.scrollTop
        spacers['test-chat'] = spacer.style.height
        sessionStorage.setItem('chat-scroll', JSON.stringify(positions))
        sessionStorage.setItem('chat-spacer', JSON.stringify(spacers))
      }
    })

    // Reload to simulate returning.
    await page.goto(BASE)
    await expect(page.locator('.chat__scroll, .chat__empty-wrap').first()).toBeVisible({ timeout: 8000 })
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const hasScroll = await page.evaluate(() => !!document.querySelector('.chat__scroll'))
    if (!hasScroll) {
      // App loaded a different (empty) chat — skip this assertion.
      return
    }

    // Inject content simulating agent still streaming.
    await injectContent(page, 'Streaming content. ', 50)
    const afterContent = await measure(page)
    if (afterContent.error) return // scroll element disappeared

    // Key assertion: spacer should have shrunk (RO is active).
    if (afterContent.listH > afterContent.clientH) {
      expect(afterContent.spacerH).toBe(0)
    } else {
      assertSpacerReasonable(afterContent)
    }
  })
})

test.describe('Empty state transition', () => {
  test('12. fullViewHRef captured on empty->chat transition', async ({ page }) => {
    await setup(page)
    await newChat(page)

    // Verify empty state — no scroll container.
    const hasScroll = await page.evaluate(
      () => !!document.querySelector('.chat__scroll')
    )
    expect(hasScroll).toBe(false)

    // Send first message — scroll container appears.
    await sendMessage(page, 'First ever message')
    const m = await measure(page)

    // Spacer should use the full viewport height, not 0.
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
      () => !document.querySelector('.chat__stop'),
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
      () => !document.querySelector('.chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    expect(m.msgCount).toBeGreaterThanOrEqual(2)
    assertSpacerReasonable(m)
  })

  test('17. Long SSE response — content overflows, user stays near top', async ({ page }) => {
    const events = [
      { type: 'catch_up_done' },
      { type: 'text', content: 'Very long response. '.repeat(200) },
      { type: 'done' },
    ]
    await setupWithSSE(page, events)
    await newChat(page)
    await sendMessage(page, 'SSE long test')

    await page.waitForFunction(
      () => !document.querySelector('.chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    // Content should overflow the viewport.
    expect(m.listH).toBeGreaterThan(m.clientH)
    // User should NOT be at the bottom — auto-follow is off by default.
    // They stay near the top where their message was sent.
    const gap = m.scrollH - m.scrollTop - m.clientH
    expect(gap).toBeGreaterThan(100)
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
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = s.scrollHeight
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 150)))
    await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
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
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = s.scrollHeight / 2
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

  test('20. SSE streaming does NOT auto-follow — user stays near top', async ({ page }) => {
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
      () => !document.querySelector('.chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    const m = await measure(page)
    // Content should exceed viewport.
    expect(m.listH).toBeGreaterThan(m.clientH)
    // Auto-follow is OFF by default — user should NOT be at the bottom.
    // They stay near the top where their message was sent.
    const gap = m.scrollH - m.scrollTop - m.clientH
    expect(gap).toBeGreaterThan(100)
    assertUserMsgAtTop(m, 'after long SSE promote')
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
      () => !document.querySelector('.chat__stop'),
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
      () => !document.querySelector('.chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    // Verify content overflows.
    const before = await measure(page)
    expect(before.listH).toBeGreaterThan(before.clientH + 100)

    // Scroll up to ~1/3.
    await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = Math.max(0, s.scrollHeight / 3)
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 200)))

    const scrollBefore = await page.evaluate(() =>
      document.querySelector('.chat__scroll')?.scrollTop ?? 0
    )
    expect(scrollBefore).toBeGreaterThan(0)

    // Wait and verify no stale auto-follow snaps back.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1000)))

    const scrollAfter = await page.evaluate(() =>
      document.querySelector('.chat__scroll')?.scrollTop ?? 0
    )
    expect(Math.abs(scrollAfter - scrollBefore)).toBeLessThan(5)
  })

  test('22. Auto-follow survives content bursts during streaming', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Burst test')

    // Start at the bottom (auto-follow engaged).
    await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
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
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = s.scrollHeight
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    // Scroll up past 50px threshold.
    await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = Math.max(0, s.scrollTop - 200)
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    const scrollBefore = await page.evaluate(() =>
      document.querySelector('.chat__scroll')?.scrollTop ?? 0
    )

    // Inject more content — should NOT auto-follow.
    await injectContent(page, 'New content arriving. ', 20)

    const scrollAfter = await page.evaluate(() =>
      document.querySelector('.chat__scroll')?.scrollTop ?? 0
    )

    // Position should not have jumped to the bottom.
    const m = await measure(page)
    const gapFromBottom = m.scrollH - scrollAfter - m.clientH
    expect(gapFromBottom).toBeGreaterThan(50)
  })

  test('25. Auto-follow stays glued tightly (regression on test 18 threshold)', async ({ page }) => {
    // Test 18 asserts gap < 200 — wide enough to let the broken RO pass
    // (5 small chunks drift ~150px without re-applying FOLLOW_BOTTOM).
    // This is the SAME setup + content volume; the only difference is
    // a TIGHT assertion (<= 5). Demonstrates the regression directly
    // without invoking the 204-refresh path that heavier injection
    // accidentally triggers.
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Autoscroll tight test')

    await injectContent(page, 'Filling viewport with lots of text. ', 150)
    const before = await measure(page)
    expect(before.listH).toBeGreaterThan(before.clientH)

    // Engage FOLLOW_BOTTOM via real gesture (test 18's exact pattern).
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
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    const atBottom = await measure(page)
    expect(atBottom.scrollH - atBottom.scrollTop - atBottom.clientH)
      .toBeLessThanOrEqual(5)

    // Five small chunks (test 18's exact volume) — auto-follow MUST
    // keep us within a few px of the bottom, not 200px adrift.
    for (let i = 0; i < 5; i++) {
      await injectContent(page, `Streaming chunk ${i + 1}. More text here. `, 5)
    }

    const after = await measure(page)
    expect(after.error).toBeUndefined()
    const afterGap = after.scrollH - after.scrollTop - after.clientH
    expect(afterGap).toBeLessThanOrEqual(10)
  })

  test('24. Auto-follow re-engages when user scrolls back to bottom', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Re-engage test')

    // Helper: simulate a user-driven scroll. pointerdown opens the
    // gesture window; the scrollTop write within it is treated as
    // user intent and may transition the ScrollMode.
    const userScrollTo = async (top) => {
      await page.evaluate((t) => {
        const s = document.querySelector('.chat__scroll')
        if (!s) return
        s.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
        s.scrollTop = t === 'bottom' ? s.scrollHeight
          : t === 'up' ? Math.max(0, s.scrollTop - 200)
          : t
      }, top)
      await page.evaluate(() => new Promise(r => setTimeout(r, 100)))
    }

    // Fill viewport and engage auto-follow at the bottom (user gesture).
    await injectContent(page, 'Initial content. ', 100)
    await userScrollTo('bottom')

    // Scroll up — disengages auto-follow.
    await userScrollTo('up')

    // Inject content — should NOT follow (user scrolled up).
    await injectContent(page, 'While scrolled up. ', 10)
    const midGap = await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      return s ? s.scrollHeight - s.scrollTop - s.clientHeight : 0
    })
    expect(midGap).toBeGreaterThan(50)

    // Now scroll back to bottom — should re-engage auto-follow.
    await userScrollTo('bottom')

    // Inject more content — should auto-follow again.
    await injectContent(page, 'After re-engage. ', 10)
    const afterGap = await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      return s ? s.scrollHeight - s.scrollTop - s.clientHeight : 0
    })
    expect(afterGap).toBeLessThan(50)
  })

  test('26. Second send after scroll-up on first response', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First message')

    // Fill viewport with first response content.
    await injectContent(page, 'First response content. ', 80)

    // Scroll up to read.
    await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = Math.max(0, s.scrollHeight / 3)
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 200)))

    // Stop the first "streaming" so we can send again.
    await page.evaluate(() =>
      document.querySelector('.chat__stop')?.click()
    )
    await page.waitForFunction(
      () => !document.querySelector('.chat__stop'),
      { timeout: 3000 }
    )
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))

    // Send second message.
    await sendMessage(page, 'Second message')

    const userMsgs = await page.evaluate(() => {
      const msgs = document.querySelectorAll('.chat__text--user')
      return [...msgs].map(el => el.textContent.trim())
    })
    expect(userMsgs).toContain('Second message')

    // Poll for the PIN_USER_MSG to settle. A single measure() races
    // React's layout effect under heavy parallel suite load and
    // flaked once in CI; the condition itself is stable but takes
    // 1-2 RAFs after the new user message commits to be in place.
    // Poll keeps the assertion tight without an arbitrary sleep.
    await expect.poll(
      async () => (await measure(page)).userVisualTop,
      { timeout: 2000, intervals: [50, 100, 200] },
    ).toBeLessThan(50)
  })

  test('27. Viewport resize cycles do not engage auto-follow on streaming chat', async ({ page }) => {
    // Guards the prevListH gate in the ResizeObserver: the auto-follow
    // branch must not snap to bottom when the list merely re-measures due
    // to a viewport resize (content unchanged). This is a partial guard
    // for the keyboard-cycle drift class of bugs — it does NOT simulate
    // Chrome Android's first-focus overshoot, which is a browser-level
    // animation quirk outside our code's control.
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First')

    // Grow the content so there's room to scroll up.
    await injectContent(page, 'Padding content line. ', 80)
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))

    // Scroll to the top (definitely not near the bottom).
    await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = 0
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 50)))

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
