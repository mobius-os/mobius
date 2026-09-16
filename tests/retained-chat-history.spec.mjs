/**
 * Core frontend behavior tests.
 *
 * Tests message rendering, input behavior, theme switching, and app canvas.
 * All tests use API interception — no agent tokens consumed.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/frontend.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { mockPendingQuestionState } from './_mockPendingQuestion.mjs'
import { createMockChatRuntime } from './_mockChatRuntime.mjs'
import { applyApp } from './app-source.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Per-worker cleanup: see tests/_chatTracker.mjs.
attachCleanup()

function fulfillStartedPost(route) {
  if (route.request().method() !== 'POST') return route.continue()
  return route.fulfill({ status: 202, body: '{"status":"started"}' })
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function setup(page, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)

  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    fulfillStartedPost(route)
  )
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
    route.fulfill({ status: 204, body: '' })
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    undefined, { timeout: 10000 }
  )
}

async function newChat(page) {
  // Worker-tagged title so cleanupWorkerChats can find + delete this
  // chat at the end of the spec. See tests/_chatTracker.mjs.
  const chat = await createTaggedChat(page)
  if (chat?.id) {
    // A valid persisted workspace wins over the legacy active-chat mirror.
    // Navigate through the supported in-scope cold deep-link contract.
    await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
      waitUntil: 'domcontentloaded',
    })
  } else {
    await page.evaluate(() => {
      document.querySelector('.drawer__item--new')?.click()
    })
  }
  await expect(page.locator('[data-chat-surface="painted"] .chat__empty-wrap')).toBeVisible({ timeout: 8000 })
  return chat
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  await expect(page.locator('[data-chat-surface="painted"] .chat__msg--user').first()).toBeVisible({ timeout: 8000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))
}

async function waitForChatMode(page, chatId, kind, timeout = 3000) {
  await expect.poll(
    () => page.evaluate(id => {
      let mode = null
      try {
        mode = JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id] || null
      } catch {}
      return {
        kind: mode?.kind || null,
        // The controller trace contains only event names, coarse geometry, and
        // redacted mode shapes. Returning it here makes a future ownership race
        // diagnosable from the assertion artifact without exposing chat text or
        // stable message identities.
        diagnostics: window.__mobiusChatScrollTrace || null,
      }
    }, chatId),
    {
      timeout,
      message: `chat ${chatId} should persist scroll mode ${kind}`,
    },
  ).toMatchObject({ kind })
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

test.describe('Input behavior', () => {









})

test.describe('Message rendering', () => {













})

test.describe('Theme switching', () => {



})

test.describe('App canvas', () => {

})

test.describe('Scroll position', () => {
  test('10. PageUp position saved on navigate, restored on return', async ({ page }) => {
    await setup(page)
    await newChat(page)

    const chatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(chatId).toBeTruthy()

    const textBlocks = Array.from({ length: 36 }, (_, i) => ({
      type: 'text',
      content: `Scroll restore paragraph ${i + 1}. ${'Persisted content. '.repeat(10)}`,
    }))
    const messages = [
      { role: 'user', content: 'Scroll restore prompt', ts: 1700000000000 },
      {
        role: 'assistant',
        content: textBlocks.map(block => block.content).join('\n\n'),
        blocks: [
          ...textBlocks,
          {
            type: 'tool',
            tool: 'Bash',
            input: 'verify keyboard scrolling',
            output: 'done',
            status: 'done',
            tool_use_id: 'keyboard-scroll-tool',
          },
        ],
      },
    ]
    const runtime = createMockChatRuntime()

    // Scroll restore is only meaningful for content that survives navigation.
    // The shared POST stub creates optimistic rows only, so this test serves
    // the persisted transcript directly and keeps the invariant under test
    // focused on the durable reading-position save/restore path.
    await page.route(new RegExp(`/api/chats/${chatId}\\?limit=`), route => {
      if (route.request().method() !== 'GET') return route.continue()
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(runtime.detail({
          messages,
          total: messages.length,
          offset: 0,
        })),
      })
    })

    await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => {
        const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.scrollHeight > el.clientHeight + 100
          && el.textContent.includes('Scroll restore paragraph 36')
      },
      undefined, { timeout: 10000 }
    )

    // Use the same browser-owned delayed key scroll that exposed the bug. A
    // synthetic scrollTop write would bypass the ownership race entirely.
    const initialTop = await page.evaluate(
      () => document.querySelector('[data-chat-surface="painted"] .chat__scroll')?.scrollTop ?? 0,
    )
    await page.getByRole('button', {
      name: 'Ran verify keyboard scrolling',
      exact: true,
    }).focus()
    await page.keyboard.press('PageUp')
    await expect.poll(
      () => page.evaluate(() => (
        document.querySelector('[data-chat-surface="painted"] .chat__scroll')?.scrollTop ?? 0
      )),
      { timeout: 3000 },
    ).toBeLessThan(initialTop - 100)
    await waitForChatMode(page, chatId, 'ANCHOR_AT')
    const scrollBefore = await page.evaluate(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return el ? el.scrollTop : null
    })
    expect(scrollBefore).toBeGreaterThan(0)
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))

    // Navigating away unmounts ChatView, which is the lifecycle boundary that
    // saves the reader's ANCHOR_AT mode for this chat.
    await page.getByLabel('Toggle navigation').click()
    await expect(page.locator('.drawer.drawer--open')).toBeVisible({ timeout: 3000 })
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    await expect(page.locator('.settings')).toBeVisible({ timeout: 5000 })
    await waitForChatMode(page, chatId, 'ANCHOR_AT')

    // Return through the app navigation stack so ChatView remounts and consumes
    // the saved mode for the same chat.
    await page.evaluate(() => history.back())
    await page.waitForFunction(
      () => {
        const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.scrollHeight > el.clientHeight + 100
          && el.textContent.includes('Scroll restore paragraph 36')
      },
      undefined, { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))

    const scrollAfter = await page.evaluate(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return el ? el.scrollTop : null
    })
    expect(Math.abs(scrollAfter - scrollBefore)).toBeLessThan(50)
  })

  test('10b. Leaving auto-scroll restores the exact old tail, not content grown while away', async ({ page }) => {
    await setup(page)
    await newChat(page)

    const chatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(chatId).toBeTruthy()

    let messages = [
      {
        cid: 'follow-restore-cid',
        role: 'user',
        content: 'Follow restore prompt',
        ts: 1700000100000,
      },
      {
        role: 'assistant',
        ts: 1700000100001,
        content: Array.from({ length: 32 }, (_, i) =>
          `Initial follow paragraph ${i + 1}. ${'Existing content. '.repeat(10)}`
        ).join('\n\n'),
        blocks: Array.from({ length: 32 }, (_, i) => ({
          type: 'text',
          content: `Initial follow paragraph ${i + 1}. ${'Existing content. '.repeat(10)}`,
        })),
      },
    ]
    const runtime = createMockChatRuntime()

    await page.route(new RegExp(`/api/chats/${chatId}\\?limit=`), route => {
      if (route.request().method() !== 'GET') return route.continue()
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(runtime.detail({
          messages,
          total: messages.length,
          offset: 0,
        })),
      })
    })

    await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => {
        const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.scrollHeight > el.clientHeight + 100
          && el.textContent.includes('Initial follow paragraph 32')
      },
      undefined, { timeout: 10000 },
    )

    // A real wheel gesture is the sole transition into FOLLOW_BOTTOM. The
    // initial restore can already place the viewport at the physical bottom,
    // so first move away from it; a wheel-down from an already-clamped tail
    // emits no scroll event and therefore cannot establish reader intent.
    const scroll = page.locator('[data-chat-surface="painted"] .chat__scroll')
    await scroll.hover()
    await page.mouse.wheel(0, -300)
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return !!el
        && el.scrollTop > 0
        && el.scrollHeight - el.scrollTop - el.clientHeight > 100
    }, undefined, { timeout: 3000 })
    await page.mouse.wheel(0, 100000)
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return !!el && el.scrollHeight - el.scrollTop - el.clientHeight < 50
    }, undefined, { timeout: 3000 })
    await waitForChatMode(page, chatId, 'FOLLOW_BOTTOM')
    const scrollBefore = await page.evaluate(
      () => document.querySelector('[data-chat-surface="painted"] .chat__scroll')?.scrollTop ?? null,
    )
    expect(scrollBefore).toBeGreaterThan(0)

    await page.getByLabel('Toggle navigation').click()
    await expect(page.locator('.drawer.drawer--open')).toBeVisible({ timeout: 3000 })
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    await expect(page.locator('.settings')).toBeVisible({ timeout: 5000 })
    await waitForChatMode(page, chatId, 'ANCHOR_AT')

    // Grow the same assistant row while the chat is inactive. A restored
    // FOLLOW_BOTTOM would jump to this new tail; the saved anchor must not.
    const grownBlocks = Array.from({ length: 18 }, (_, i) => ({
      type: 'text',
      content: `Grown while away marker ${i + 1}. ${'New content. '.repeat(10)}`,
    }))
    messages = [
      messages[0],
      {
        ...messages[1],
        content: `${messages[1].content}\n\n${grownBlocks.map(b => b.content).join('\n\n')}`,
        blocks: [...messages[1].blocks, ...grownBlocks],
      },
    ]

    await page.evaluate(() => history.back())
    await page.waitForFunction(
      () => {
        const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.textContent.includes('Grown while away marker 18')
      },
      undefined, { timeout: 10000 },
    )
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))))

    const restored = await page.evaluate(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      return el ? {
        scrollTop: el.scrollTop,
        bottomGap: el.scrollHeight - el.scrollTop - el.clientHeight,
      } : null
    })
    expect(restored).not.toBeNull()
    expect(Math.abs(restored.scrollTop - scrollBefore)).toBeLessThan(50)
    expect(restored.bottomGap).toBeGreaterThan(100)
  })

  test('10c. A paginated return anchor survives the latest-page refresh', async ({ page }) => {
    await setup(page)
    await newChat(page)

    const chatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(chatId).toBeTruthy()

    const allMessages = Array.from({ length: 45 }, (_, index) => {
      const role = index % 2 === 0 ? 'user' : 'assistant'
      const content = `History row ${index}. ${'Restorable content. '.repeat(8)}`
      return {
        cid: role === 'user' ? `history-cid-${index}` : undefined,
        role,
        ts: 1700000200000 + index,
        content,
        blocks: role === 'assistant' ? [{ type: 'text', content }] : [],
      }
    })
    let recentFetches = 0
    const runtime = createMockChatRuntime()

    await page.route(new RegExp(`/api/chats/${chatId}\\?limit=`), async route => {
      if (route.request().method() !== 'GET') return route.continue()
      const url = new URL(route.request().url())
      const limit = Number(url.searchParams.get('limit') || 20)
      const beforeParam = url.searchParams.get('before')
      const before = beforeParam == null ? allMessages.length : Number(beforeParam)
      const start = Math.max(0, before - limit)
      if (beforeParam == null) recentFetches += 1
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(runtime.detail({
          messages: allMessages.slice(start, before),
          total: allMessages.length,
          offset: start,
        })),
      })
    })

    await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => document.querySelector('[data-key="history-cid-44"]'),
      undefined, { timeout: 10000 },
    )
    // Older pages now prefetch from the reader's near-top gesture instead of
    // exposing a manual Load button. Drive that owning interaction directly.
    await page.evaluate(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (!el) throw new Error('missing paginated scroll surface')
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      el.scrollTop = 0
      el.dispatchEvent(new Event('scroll', { bubbles: true }))
    })
    await page.waitForFunction(
      () => document.querySelector('[data-key="history-cid-10"]'),
      undefined, { timeout: 5000 },
    )
    // The prepend and its viewport compensation complete in one task. Wait for
    // the resulting layout to settle before synthesizing the next reader
    // gesture so this return-location check starts from stable geometry.
    await page.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve))))

    // Read an older row that is outside the server's default newest-20 page.
    // The pointerdown makes the ensuing scroll an owner gesture, so R4 saves
    // this exact row+offset rather than a programmatic position.
    await page.evaluate(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const target = document.querySelector('[data-key="history-cid-10"]')
      if (!el || !target) throw new Error('missing paginated anchor target')
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      el.scrollTop = target.offsetTop + 12
    })
    await page.waitForFunction(
      id => JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id]?.key
        === 'history-cid-10',
      chatId,
      { timeout: 3000 },
    )

    await page.getByLabel('Toggle navigation').click()
    await expect(page.locator('.drawer.drawer--open')).toBeVisible({ timeout: 3000 })
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    await expect(page.locator('.settings')).toBeVisible({ timeout: 5000 })

    await page.evaluate(() => history.back())
    await expect.poll(() => recentFetches, { timeout: 10000 }).toBeGreaterThan(1)
    await page.waitForFunction(
      () => {
        const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        return !!el && getComputedStyle(el).visibility !== 'hidden'
      },
      undefined, { timeout: 10000 },
    )
    await page.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve))))

    const restored = await page.evaluate(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const target = document.querySelector('[data-key="history-cid-10"]')
      return {
        keyStillMounted: !!target,
        offset: target && el ? target.offsetTop - el.scrollTop : null,
        scrollTop: el?.scrollTop ?? null,
      }
    })
    expect(restored.keyStillMounted).toBe(true)
    expect(Math.abs(restored.offset - (-12))).toBeLessThanOrEqual(2)
    expect(restored.scrollTop).toBeGreaterThan(0)
  })

  test('10d. Running chat presents before catch-up, then settles without movement', async ({ page }) => {
    await setup(page, { width: 900, height: 760 })
    await newChat(page)

    const chatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(chatId).toBeTruthy()

    // This test overlays a populated transcript through a route mock. Persist a
    // small occupancy marker too so the New Chat action owns a genuinely new
    // draft instead of correctly reusing the otherwise untouched server row.
    const token = await page.evaluate(() => localStorage.getItem('token'))
    expect(token).toBeTruthy()
    const occupyResponse = await page.request.put(`${BASE}/api/chats/${chatId}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { messages: [{ role: 'user', content: 'Entry restoration fixture' }] },
    })
    expect(occupyResponse.ok()).toBe(true)

    let returning = false
    const runtime = createMockChatRuntime()
    let streamCount = 0
    let catchUpServed = false
    let releaseCatchUp
    const catchUpGate = new Promise(resolve => { releaseCatchUp = resolve })
    let returnImageServed = false
    const squarePng = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII=',
      'base64',
    )
    const history = imageName => {
      const above = [
        ...Array.from({ length: 32 }, (_, i) =>
          `Entry-settle paragraph ${i + 1}. ${'Content above the saved anchor. '.repeat(8)}`),
        `![late layout image](${BASE}/${imageName})`,
      ].join('\n\n')
      const below = Array.from({ length: 24 }, (_, i) =>
        `Later paragraph ${i + 1}. ${'Content below the saved anchor. '.repeat(8)}`).join('\n\n')
      return [
        { id: 'entry-user-1', cid: 'entry-cid-1', role: 'user', ts: 1700000300000, content: 'Entry test start' },
        { id: 'entry-above', role: 'assistant', ts: 1700000300001, content: above, blocks: [{ type: 'text', content: above }] },
        { id: 'entry-anchor', cid: 'entry-anchor-cid', role: 'user', ts: 1700000300002, content: 'Saved reading anchor' },
        { id: 'entry-tail', role: 'assistant', ts: 1700000300003, content: below, blocks: [{ type: 'text', content: below }] },
      ]
    }

    await page.route(new RegExp(`/api/chats/${chatId}\\?limit=`), async route => {
      if (route.request().method() !== 'GET') return route.continue()
      if (returning) await new Promise(resolve => setTimeout(resolve, 220))
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(runtime.detail({
          messages: history(returning ? 'entry-image-return.png' : 'entry-image-initial.png'),
          total: 4,
          offset: 0,
        })),
      })
    })
    await page.route('**/entry-image-initial.png', route => route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' },
      body: squarePng,
    }))
    await page.route('**/entry-image-return.png', async route => {
      await new Promise(resolve => setTimeout(resolve, 320))
      returnImageServed = true
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' },
        body: squarePng,
      })
    })
    await page.route(new RegExp(`/api/chats/${chatId}/stream$`), async route => {
      streamCount += 1
      if (streamCount > 1) return route.fulfill({ status: 204, body: '' })
      await catchUpGate
      catchUpServed = true
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: [
          'data: {"type":"catch_up_done"}\n\n',
          'data: {"type":"text","content":"Still active after mount catch-up"}\n\n',
        ].join(''),
      })
    })

    // First visit: establish a deliberate saved ANCHOR_AT location.
    await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const img = document.querySelector('[data-chat-surface="painted"] .md-image')
      return !!el && getComputedStyle(el).visibility !== 'hidden'
        && !!img && !!document.querySelector('[data-key="entry-anchor"]')
    }, undefined, { timeout: 10000 })
    // This fixture image sits after a deliberately tall prefix and therefore
    // remains outside Chromium's native lazy-load range at the initial tail.
    // Bring it into range before recording the settled reading coordinate.
    await page.locator('[data-chat-surface="painted"] .md-image').scrollIntoViewIfNeeded()
    await page.waitForFunction(() => {
      const img = document.querySelector('[data-chat-surface="painted"] .md-image')
      return !!img?.complete && img.naturalWidth > 0
    }, undefined, { timeout: 10000 })
    await page.evaluate(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const target = document.querySelector('[data-key="entry-anchor"]')
      if (!el || !target) throw new Error('missing entry anchor')
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      // Put the target just above the viewport edge so it becomes the
      // controller's topmost visible row. Leaving it 80px below the edge made
      // the preceding, very tall assistant row the saved anchor instead.
      el.scrollTop = target.offsetTop + 12
      el.dispatchEvent(new Event('scroll', { bubbles: true }))
    })
    await page.waitForFunction(
      id => JSON.parse(localStorage.getItem('chat-reading-position') || '{}')[id]?.key
        === 'entry-anchor',
      chatId,
      { timeout: 3000 },
    )

    await page.getByLabel('Toggle navigation').click()
    await expect(page.locator('.drawer.drawer--open')).toBeVisible({ timeout: 3000 })
    await page.getByLabel('Primary navigation')
      .getByRole('button', { name: 'New chat', exact: true })
      .click()
    await expect(page.locator('[data-chat-surface="painted"] .chat__empty-wrap'))
      .toBeVisible({ timeout: 5000 })
    const decoyChatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(decoyChatId).toBeTruthy()
    expect(decoyChatId).not.toBe(chatId)

    returning = true
    runtime.update({ running: true })
    await page.evaluate(() => {
      window.__entryTrajectory = []
      const started = performance.now()
      const sample = () => {
        const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        const target = document.querySelector('[data-key="entry-anchor"]')
        const visible = !!el && getComputedStyle(el).visibility !== 'hidden'
        const held = !!document.querySelector('.shell__chat-view--held')
        const activeLayer = document.querySelector(
          '.shell__chat-view.shell__view--active',
        )
        const activeFrame = !!activeLayer && (
          !!activeLayer.querySelector('.chat__empty-wrap')
          || (() => {
            const activeScroll = activeLayer.querySelector('.chat__scroll')
            return !!activeScroll
              && getComputedStyle(activeScroll).visibility !== 'hidden'
          })()
        )
        window.__entryTrajectory.push({
          t: Math.round(performance.now() - started),
          visible,
          painted: held || activeFrame,
          held,
          anchor: !!target,
          y: el && target
            ? Math.round(target.getBoundingClientRect().top - el.getBoundingClientRect().top)
            : null,
        })
        if (performance.now() - started < 1500) requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
      history.back()
    })

    await page.waitForFunction(
      id => localStorage.getItem('moebius_active_chat') === id,
      chatId,
      { timeout: 3000 },
    )
    await page.waitForFunction(id => {
      const painted = document.querySelector(
        `[data-chat-surface="painted"][data-chat-id="${id}"]`,
      )
      const el = painted?.querySelector('.chat__scroll')
      const target = painted?.querySelector('[data-key="entry-anchor"]')
      return !!el && getComputedStyle(el).visibility !== 'hidden' && !!target
    }, chatId, { timeout: 5000 })
    await expect(page.locator('.shell__chat-view--held')).toHaveCount(0)
    expect(catchUpServed).toBe(false)
    releaseCatchUp()
    await expect.poll(() => catchUpServed, { timeout: 3000 }).toBe(true)

    await page.waitForFunction(() => {
      const el = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const img = document.querySelector('[data-chat-surface="painted"] .md-image')
      return !!el && getComputedStyle(el).visibility !== 'hidden'
        && !!img?.src.includes('entry-image-return.png') && !!img.complete
        && !!document.querySelector('[data-key="entry-anchor"]')
    }, undefined, { timeout: 10000 })

    const trajectory = await page.evaluate(() => window.__entryTrajectory || [])
    const visibleRows = trajectory.filter(row => row.visible)
    expect(returnImageServed).toBe(true)
    expect(streamCount).toBeGreaterThan(0)
    expect(trajectory.some(row => row.held)).toBe(true)
    expect(trajectory.every(row => row.painted)).toBe(true)
    expect(visibleRows.length).toBeGreaterThan(2)
    expect(visibleRows.every(row => row.anchor && row.y != null)).toBe(true)
    const visibleYs = visibleRows.map(row => row.y)
    expect(Math.max(...visibleYs) - Math.min(...visibleYs)).toBeLessThanOrEqual(2)
  })
})

// ---------------------------------------------------------------------------
// Enter key behavior across device types
// ---------------------------------------------------------------------------

test.describe('Enter key — touch-primary device (mobile)', () => {
  // Emulate a touch-primary device: hasTouch=true makes Chromium report
  // (hover: none) and (pointer: coarse) via matchMedia, which is what
  // the keydown handler checks.
  test.use({ hasTouch: true })


})

test.describe('Enter key — desktop (no touch)', () => {
  // Default Playwright context: no touch, hover: hover, pointer: fine.
  // Enter should send the message.




})

// ---------------------------------------------------------------------------
// Scroll preservation after stream completion
// ---------------------------------------------------------------------------

test.describe('Scroll after stream end', () => {

})

// ---------------------------------------------------------------------------
// Connection recovery — re-fetch messages on 204 reconnect
// ---------------------------------------------------------------------------

test.describe('Connection recovery', () => {



})
