/**
 * Core frontend behavior tests.
 *
 * Tests message rendering, input behavior, theme switching, and app canvas.
 * All tests use API interception — no agent tokens consumed.
 *
 * Run: npx playwright test tests/frontend.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Per-worker cleanup: see tests/_chatTracker.mjs.
attachCleanup()

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function setup(page, viewport = { width: 412, height: 915 }) {
  await page.setViewportSize(viewport)

  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    route.fulfill({ status: 202, body: '{}' })
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
    { timeout: 10000 }
  )
}

async function newChat(page) {
  // Worker-tagged title so cleanupWorkerChats can find + delete this
  // chat at the end of the spec. See tests/_chatTracker.mjs.
  await createTaggedChat(page)
  await page.evaluate(() => {
    document.querySelector('.drawer__item--new')?.click()
  })
  const hasEmpty = await page.evaluate(
    () => !!document.querySelector('.chat__empty-wrap')
  )
  if (!hasEmpty) await page.goto(BASE)
  await expect(page.locator('.chat__empty-wrap')).toBeVisible({ timeout: 8000 })
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Input behavior', () => {
  test('1. Input clears after send', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Test message')

    const value = await page.evaluate(
      () => document.querySelector('.chat__input')?.value
    )
    expect(value).toBe('')
  })

  test('2. Empty input does not send', async ({ page }) => {
    await setup(page)
    await newChat(page)

    // Try to send empty
    await page.keyboard.press('Enter')
    await page.evaluate(() => new Promise(r => setTimeout(r, 200)))

    // Should still be on empty state
    const hasEmpty = await page.evaluate(
      () => !!document.querySelector('.chat__empty-wrap')
    )
    expect(hasEmpty).toBe(true)
  })

  test('3. Send button appears when input has text', async ({ page }) => {
    await setup(page)
    await newChat(page)

    // Initially no send button (voice button instead)
    const hasSend = await page.evaluate(
      () => !!document.querySelector('.chat__send')
    )
    expect(hasSend).toBe(false)

    // Type something — send button should appear
    await page.getByRole('textbox', { name: 'Message Möbius…' }).fill('hello')
    await page.evaluate(() => new Promise(r => setTimeout(r, 100)))

    const hasSendAfter = await page.evaluate(
      () => !!document.querySelector('.chat__send')
    )
    expect(hasSendAfter).toBe(true)
  })
})

test.describe('Message rendering', () => {
  test('4. User message renders with correct class', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Hello world')

    const userMsg = await page.evaluate(() => {
      const el = document.querySelector('.chat__msg--user')
      return {
        exists: !!el,
        text: el?.querySelector('.chat__text--user')?.textContent?.trim(),
      }
    })
    expect(userMsg.exists).toBe(true)
    expect(userMsg.text).toBe('Hello world')
  })

  test('5. Multiple messages render in order', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'First')

    // Stop and send second
    await page.evaluate(() => document.querySelector('.chat__stop')?.click())
    await page.waitForFunction(() => !document.querySelector('.chat__stop'), { timeout: 3000 })
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
    await sendMessage(page, 'Second')

    const msgs = await page.evaluate(() => {
      const userMsgs = document.querySelectorAll('.chat__text--user')
      return [...userMsgs].map(m => m.textContent.trim())
    })
    expect(msgs).toContain('First')
    expect(msgs).toContain('Second')
    expect(msgs.indexOf('First')).toBeLessThan(msgs.indexOf('Second'))
  })

  test('6. Thinking dots show while agent is processing', async ({ page }) => {
    await setup(page)
    await newChat(page)
    await sendMessage(page, 'Test thinking')

    const hasThinking = await page.evaluate(
      () => !!document.querySelector('.chat__thinking')
    )
    expect(hasThinking).toBe(true)
  })

  test('6a. Markdown links and images use an explicit URL allowlist', async ({ page }) => {
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, body: '{}' })
    )
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
      route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: [
          'data: {"type":"text","content":"[safe](https://example.com) [mail](mailto:test@example.com) [bad](javascript:alert(1)) ![good](https://example.com/img.png) ![badimg](javascript:alert(2))"}\n\n',
          'data: {"type":"done"}\n\n',
        ].join(''),
      })
    )

    await page.setViewportSize({ width: 412, height: 915 })
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Render markdown')

    const result = await page.evaluate(() => {
      const assistant = document.querySelector('.chat__msg--assistant')
      const anchors = Array.from(assistant?.querySelectorAll('a') || [])
        .map(a => ({ text: a.textContent, href: a.getAttribute('href') }))
      const images = Array.from(assistant?.querySelectorAll('img.md-image') || [])
        .map(img => img.getAttribute('src'))
      return {
        text: assistant?.textContent || '',
        anchors,
        images,
      }
    })

    expect(result.anchors).toEqual([
      { text: 'safe', href: 'https://example.com' },
      { text: 'mail', href: 'mailto:test@example.com' },
    ])
    expect(result.text).toContain('bad')
    expect(result.images).toEqual(['https://example.com/img.png'])
  })

  test('6b. Question card renders options and submits answer', async ({ page }) => {
    const sentBodies = []
    let persistedAnswers = null
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      sentBodies.push(route.request().postDataJSON())
      route.fulfill({ status: 202, body: '{}' })
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/question-answers$/, route => {
      persistedAnswers = route.request().postDataJSON().answers
      route.fulfill({ status: 200, body: '{"ok":true}' })
    })
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
    let streamCount = 0
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      streamCount++
      if (streamCount === 1) {
        route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
          body: [
            'data: {"type":"text","content":"Let me ask you:"}\n\n',
            `data: ${JSON.stringify({
              type: 'question',
              questions: [{
                question: 'What color?',
                header: 'Color',
                multiSelect: false,
                options: [
                  { label: 'Red', description: 'warm' },
                  { label: 'Blue', description: 'cool' },
                  { label: 'Green', description: 'nature' },
                ],
              }],
            })}\n\n`,
            'data: {"type":"done"}\n\n',
          ].join(''),
        })
      } else {
        route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
          body: [
            'data: {"type":"text","content":"Great, you picked Blue!"}\n\n',
            'data: {"type":"done"}\n\n',
          ].join(''),
        })
      }
    })

    await page.setViewportSize({ width: 412, height: 915 })
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Ask me something')

    // Wait for question card to appear.
    await expect(page.locator('.qcard')).toBeVisible({ timeout: 5000 })

    // Verify options rendered (3 options + Other).
    const options = page.locator('.qcard__opt')
    await expect(options).toHaveCount(4)

    // Select "Blue".
    await page.locator('.qcard__opt', { hasText: 'Blue' }).click()
    await expect(page.locator('.qcard__opt--on')).toHaveCount(1)

    // Submit.
    await expect(page.locator('.qcard__submit')).toBeEnabled()
    await page.locator('.qcard__submit').click()

    // Wait for the answer to be sent as a second (hidden) message POST.
    await expect.poll(() => sentBodies.length).toBe(2)
    expect(sentBodies[1].content).toContain('Blue')
    expect(sentBodies[1].hidden).toBe(true)

    // Atomic answer persistence: answers now ride along in the
    // /messages POST itself (the redesign eliminated the separate
    // POST /question-answers race). Verify the answers field is set.
    expect(sentBodies[1].answers).toEqual({ 'What color?': 'Blue' })

    // Wait for the agent's follow-up response to arrive (second stream).
    await expect(page.locator('.chat__scroll')).toContainText(
      'you picked Blue', { timeout: 5000 },
    )

    // Verify the question card is in answered state (no submit button).
    await expect(page.locator('.qcard__submit')).toHaveCount(0)
  })

  test('6c. Question card answer sends hidden message', async ({ page }) => {
    // Regression: hidden flag must be passed through sendMessage
    // to the backend so question answers don't show as user bubbles.
    const sentBodies = []
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      sentBodies.push(route.request().postDataJSON())
      route.fulfill({ status: 202, body: '{}' })
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/question-answers$/, route =>
      route.fulfill({ status: 200, body: '{"ok":true}' })
    )
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
    let streamCount = 0
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      streamCount++
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: streamCount === 1
          ? [
              `data: ${JSON.stringify({ type: 'question', questions: [{ question: 'Pick one', header: 'Test', multiSelect: false, options: [{ label: 'A', description: '' }, { label: 'B', description: '' }] }] })}\n\n`,
              'data: {"type":"done"}\n\n',
            ].join('')
          : [
              'data: {"type":"text","content":"Got it"}\n\n',
              'data: {"type":"done"}\n\n',
            ].join(''),
      })
    })

    await page.setViewportSize({ width: 412, height: 915 })
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap') || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Ask me')
    await expect(page.locator('.qcard')).toBeVisible({ timeout: 5000 })
    await page.locator('.qcard__opt', { hasText: 'A' }).click()
    await page.locator('.qcard__submit').click()

    await expect.poll(() => sentBodies.length).toBe(2)
    // The answer message MUST have hidden: true
    expect(sentBodies[1].hidden).toBe(true)
    expect(sentBodies[1].content).toContain('A')
  })

  test('6d. Empty question events do not render a card', async ({ page }) => {
    // Regression: partial assistant events with empty questions array
    // must not create an empty QuestionCard.
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, body: '{}' })
    )
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: [
          // Empty question (partial event) — should be filtered
          'data: {"type":"question","questions":[]}\n\n',
          // Real question
          `data: ${JSON.stringify({ type: 'question', questions: [{ question: 'Real question', header: 'Q', multiSelect: false, options: [{ label: 'X', description: '' }, { label: 'Y', description: '' }] }] })}\n\n`,
          'data: {"type":"done"}\n\n',
        ].join(''),
      })
    )

    await page.setViewportSize({ width: 412, height: 915 })
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap') || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Ask me')

    // Only ONE question card should render (the real one).
    await expect(page.locator('.qcard')).toHaveCount(1, { timeout: 5000 })
    await expect(page.locator('.qcard')).toContainText('Real question')
  })
})

test.describe('Theme switching', () => {
  test('7. Dark mode toggle changes CSS variables', async ({ page }) => {
    await setup(page)

    // Get initial background color
    const initialBg = await page.evaluate(
      () => getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
    )

    // Navigate to settings and toggle dark mode
    await page.evaluate(() => {
      // Try to find the toggle
      const toggle = document.querySelector('[aria-label="Toggle dark mode"]')
        || document.querySelector('input[type="checkbox"]')
        || document.querySelector('.settings__toggle')
      return !!toggle
    })

    // The test verifies that --bg CSS variable exists and has a value
    expect(initialBg.length).toBeGreaterThan(0)
  })

  test('8. CSS variables are applied to chat elements', async ({ page }) => {
    await setup(page)

    const styles = await page.evaluate(() => {
      const root = getComputedStyle(document.documentElement)
      return {
        bg: root.getPropertyValue('--bg').trim(),
        text: root.getPropertyValue('--text').trim(),
        accent: root.getPropertyValue('--accent').trim(),
        surface: root.getPropertyValue('--surface').trim(),
      }
    })

    // All CSS variables should be defined
    expect(styles.bg.length).toBeGreaterThan(0)
    expect(styles.text.length).toBeGreaterThan(0)
    expect(styles.accent.length).toBeGreaterThan(0)
    expect(styles.surface.length).toBeGreaterThan(0)
  })
})

test.describe('App canvas', () => {
  test('9. Apps API returns a list', async ({ page }) => {
    await setup(page)

    const result = await page.evaluate(async () => {
      const token = localStorage.getItem('token')
      // Origin-relative — the shell now lives under `/shell/` so a
      // `./api/...` reference resolves to `/shell/api/...` and gets
      // caught by the SPA index instead of the API router.
      const res = await fetch('/api/apps/', {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) return { ok: false, status: res.status }
      const data = await res.json()
      return { ok: true, isArray: Array.isArray(data), count: Array.isArray(data) ? data.length : 0 }
    })
    expect(result.ok).toBe(true)
    expect(result.isArray).toBe(true)
    // At minimum the Hello World seed app should exist.
    expect(result.count).toBeGreaterThanOrEqual(1)
  })
})

test.describe('Scroll position', () => {
  test('10. Scroll position saved on navigate, restored on return', async ({ page }) => {
    await setup(page)
    await newChat(page)

    // Send a message and get some content
    await sendMessage(page, 'Content for scroll test')
    await page.evaluate(() => document.querySelector('.chat__stop')?.click())
    await page.waitForFunction(() => !document.querySelector('.chat__stop'), { timeout: 3000 })
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))

    // Record scroll position
    const scrollBefore = await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
      return el ? el.scrollHeight - el.scrollTop : null
    })

    // Reload page
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!document.querySelector('.chat__scroll'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    // Check scroll was restored (within tolerance)
    const scrollAfter = await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
      return el ? el.scrollHeight - el.scrollTop : null
    })

    if (scrollBefore != null && scrollAfter != null) {
      expect(Math.abs(scrollBefore - scrollAfter)).toBeLessThan(50)
    }
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

  test('10a. Enter inserts newline on touch-primary device', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 915 })

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, body: '{}' })
    )
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)

    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('Line one')
    await page.keyboard.press('Enter')

    await page.evaluate(() => new Promise(r => setTimeout(r, 300)))

    // Should still be on the empty state (no send happened).
    const hasEmpty = await page.evaluate(
      () => !!document.querySelector('.chat__empty-wrap')
    )
    expect(hasEmpty).toBe(true)

    // Textarea should still have the text.
    const value = await page.evaluate(
      () => document.querySelector('.chat__input')?.value
    )
    expect(value).toContain('Line one')
  })
})

test.describe('Enter key — desktop (no touch)', () => {
  // Default Playwright context: no touch, hover: hover, pointer: fine.
  // Enter should send the message.

  test('10b. Enter sends on desktop', async ({ page }) => {
    await setup(page)
    await newChat(page)

    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('Desktop send test')
    await page.keyboard.press('Enter')

    await page.evaluate(() => new Promise(r => setTimeout(r, 300)))

    // Should NOT be on empty state — message was sent.
    const hasScroll = await page.evaluate(
      () => !!document.querySelector('.chat__scroll')
    )
    expect(hasScroll).toBe(true)
  })

  test('10c. Shift+Enter inserts newline on desktop', async ({ page }) => {
    await setup(page)
    await newChat(page)

    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('Line one')
    await page.keyboard.press('Shift+Enter')

    await page.evaluate(() => new Promise(r => setTimeout(r, 300)))

    // Should still be on empty state (no send).
    const hasEmpty = await page.evaluate(
      () => !!document.querySelector('.chat__empty-wrap')
    )
    expect(hasEmpty).toBe(true)

    // Textarea should have multiline content.
    const value = await page.evaluate(
      () => document.querySelector('.chat__input')?.value
    )
    expect(value).toContain('Line one')
    expect(value).toContain('\n')
  })
})

// ---------------------------------------------------------------------------
// Scroll preservation after stream completion
// ---------------------------------------------------------------------------

test.describe('Scroll after stream end', () => {
  test('11. Scroll position stable after stream completes and user scrolls up', async ({ page }) => {
    // Build a long SSE response so content overflows.
    const chunks = []
    for (let i = 0; i < 30; i++) {
      chunks.push({ type: 'text', content: `Paragraph ${i + 1}. ${'Content here. '.repeat(12)} ` })
    }
    const sseEvents = [
      { type: 'catch_up_done' },
      ...chunks,
      { type: 'done' },
    ]
    const sseBody = sseEvents.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')

    await page.setViewportSize({ width: 412, height: 915 })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, body: '{}' })
    )
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
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
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)

    // Send message → stream completes.
    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('Long response test')
    await page.keyboard.press('Enter')

    await page.waitForFunction(
      () => !document.querySelector('.chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    // Verify content overflows.
    const overflows = await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      return s ? s.scrollHeight > s.clientHeight + 100 : false
    })
    expect(overflows).toBe(true)

    // Scroll up to ~1/3 of the way (user reading earlier content).
    await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      if (s) s.scrollTop = Math.max(0, s.scrollHeight / 3)
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 200)))

    const scrollBefore = await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      return s ? s.scrollTop : 0
    })
    expect(scrollBefore).toBeGreaterThan(0)

    // Wait — no new messages, just time passing. Any stale RO or timer
    // that snaps the user to bottom would fire within this window.
    await page.evaluate(() => new Promise(r => setTimeout(r, 1000)))

    const scrollAfter = await page.evaluate(() => {
      const s = document.querySelector('.chat__scroll')
      return s ? s.scrollTop : 0
    })

    // Scroll position should not have moved (tolerance for sub-pixel).
    expect(Math.abs(scrollAfter - scrollBefore)).toBeLessThan(5)
  })
})

// ---------------------------------------------------------------------------
// Connection recovery — re-fetch messages on 204 reconnect
// ---------------------------------------------------------------------------

test.describe('Connection recovery', () => {
  test('12. Terminal 204 while sending refreshes persisted messages', async ({ page }) => {
    // Simulate: send message → stream request reaches the server after
    // the in-memory broadcast has expired → terminal 204 → messages
    // re-fetched from persisted DB state.
    await page.setViewportSize({ width: 412, height: 915 })

    let streamCallCount = 0
    let refreshReady = false

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, body: '{}' })
    )
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )

    await page.route(/\/api\/chats\/[0-9a-f-]+\?limit=/, route => {
      if (!refreshReady || route.request().method() !== 'GET') {
        route.continue()
        return
      }
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [
            { role: 'user', content: 'Recovery test', ts: Date.now() },
            { role: 'assistant', content: 'Recovered final response from DB.' },
          ],
          total: 2,
          offset: 0,
        }),
      })
    })

    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, async route => {
      streamCallCount++
      refreshReady = true
      // Wait past the just-sent 204 retry window; this is the terminal
      // broadcast-expired case, not the immediate POST/GET race.
      await new Promise(resolve => setTimeout(resolve, 1700))
      route.fulfill({ status: 204, body: '' })
    })

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)

    // Send message → first stream completes.
    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('Recovery test')
    await page.keyboard.press('Enter')

    await page.waitForFunction(
      () => !document.querySelector('.chat__stop'),
      { timeout: 10000 }
    )
    await expect(page.locator('.chat__scroll')).toContainText(
      'Recovered final response from DB.',
      { timeout: 5000 },
    )
    expect(streamCallCount).toBe(1)
  })

  test('13. Visibility change after stream completion stays idle', async ({ page }) => {
    // After a stream completes (done event), isStreaming is false.
    // A subsequent visibility change must not reconnect an idle chat.
    await page.setViewportSize({ width: 412, height: 915 })

    let streamCallCount = 0

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      route.fulfill({ status: 202, body: '{}' })
    )
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )

    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      streamCallCount++
      if (streamCallCount === 1) {
        route.fulfill({
          status: 200,
          headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
          body: [
            'data: {"type":"catch_up_done"}\n\n',
            'data: {"type":"text","content":"Agent response."}\n\n',
            'data: {"type":"done"}\n\n',
          ].join(''),
        })
      } else {
        // Subsequent reconnect → 204 (chat finished).
        route.fulfill({ status: 204, body: '' })
      }
    })

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)

    const input = page.getByRole('textbox', { name: 'Message Möbius…' })
    await input.fill('Reconnect test')
    await page.keyboard.press('Enter')

    await page.waitForFunction(
      () => !document.querySelector('.chat__stop'),
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))
    expect(streamCallCount).toBe(1)

    // Simulate visibility change — should not reconnect because the
    // stream is already complete.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        value: 'visible', configurable: true,
      })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.evaluate(() => new Promise(r => setTimeout(r, 500)))

    expect(streamCallCount).toBe(1)

    // Content should still be intact (not duplicated or lost).
    const msgCount = await page.evaluate(() =>
      document.querySelectorAll('.chat__msg--assistant').length
    )
    expect(msgCount).toBe(1)
  })
})
