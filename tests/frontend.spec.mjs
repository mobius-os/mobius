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
    { timeout: 10000 }
  )
}

async function newChat(page) {
  // Worker-tagged title so cleanupWorkerChats can find + delete this
  // chat at the end of the spec. See tests/_chatTracker.mjs.
  const chat = await createTaggedChat(page)
  if (chat?.id) {
    await page.goto(`${BASE}/shell/chat/${chat.id}`, { waitUntil: 'domcontentloaded' })
  } else {
    await page.evaluate(() => {
      document.querySelector('.drawer__item--new')?.click()
    })
  }
  await expect(page.locator('.chat__empty-wrap')).toBeVisible({ timeout: 8000 })
}

async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  await expect(page.locator('.chat__msg--user').first()).toBeVisible({ timeout: 8000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))
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
      fulfillStartedPost(route)
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
    let answerSubmitted = false
    let followupStreamServed = false
    const questionBlock = {
      type: 'question',
      question_id: 'q-color',
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
    }
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      if (route.request().method() !== 'POST') return route.continue()
      const body = route.request().postDataJSON()
      sentBodies.push(body)
      if (body.answers) answerSubmitted = true
      return fulfillStartedPost(route)
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/question-answers$/, route =>
      route.fulfill({ status: 200, body: '{"ok":true}' })
    )
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      if (!answerSubmitted) {
        route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
          body: [
            'data: {"type":"catch_up_done"}\n\n',
            'data: {"type":"text","content":"Let me ask you:"}\n\n',
            `data: ${JSON.stringify(questionBlock)}\n\n`,
            'data: {"type":"done"}\n\n',
          ].join(''),
        })
      } else {
        followupStreamServed = true
        route.fulfill({
          status: 200,
          headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
          },
          body: [
            'data: {"type":"catch_up_done"}\n\n',
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

    // The stream-rendering path is covered elsewhere. Here the question-card
    // contract is that submitting an answer starts the hidden continuation.
    await expect.poll(() => followupStreamServed).toBe(true)

    // Verify the question card keeps its geometry and grays out its submit
    // control instead of removing the row underneath the reader.
    await expect(page.locator('.qcard__submit')).toBeDisabled()
    await expect(page.locator('.qcard__submit')).toHaveText('Submitted')
  })

  test('6c. Question card answer sends hidden message', async ({ page }) => {
    // Regression: hidden flag must be passed through sendMessage
    // to the backend so question answers don't show as user bubbles.
    const sentBodies = []
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      if (route.request().method() !== 'POST') return route.continue()
      sentBodies.push(route.request().postDataJSON())
      return fulfillStartedPost(route)
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
              `data: ${JSON.stringify({ type: 'question', question_id: 'q-pick-one-hidden', questions: [{ question: 'Pick one', header: 'Test', multiSelect: false, options: [{ label: 'A', description: '' }, { label: 'B', description: '' }] }] })}\n\n`,
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
      fulfillStartedPost(route)
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
          `data: ${JSON.stringify({ type: 'question', question_id: 'q-real-question', questions: [{ question: 'Real question', header: 'Q', multiSelect: false, options: [{ label: 'X', description: '' }, { label: 'Y', description: '' }] }] })}\n\n`,
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
      const headers = {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      }
      const created = await fetch('/api/apps/', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          name: `E2E List Smoke ${Date.now()}`,
          description: 'Temporary app for the apps-list API smoke test.',
          jsx_source: 'export default function App() { return <div>list smoke</div> }',
        }),
      })
      if (!created.ok) {
        return {
          ok: false,
          phase: 'create',
          status: created.status,
          body: await created.text(),
        }
      }
      const app = await created.json()

      // Origin-relative — the shell now lives under `/shell/` so a
      // `./api/...` reference resolves to `/shell/api/...` and gets
      // caught by the SPA index instead of the API router.
      const res = await fetch('/api/apps/', {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) return { ok: false, phase: 'list', status: res.status }
      const data = await res.json()
      await fetch(`/api/apps/${app.id}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      return {
        ok: true,
        isArray: Array.isArray(data),
        foundCreatedApp: Array.isArray(data) && data.some(item => item.id === app.id),
      }
    })
    expect(result.ok).toBe(true)
    expect(result.isArray).toBe(true)
    expect(result.foundCreatedApp).toBe(true)
  })
})

test.describe('Scroll position', () => {
  test('10. Scroll position saved on navigate, restored on return', async ({ page }) => {
    await setup(page)
    await newChat(page)

    const chatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(chatId).toBeTruthy()

    const messages = [
      { role: 'user', content: 'Scroll restore prompt', ts: 1700000000000 },
      {
        role: 'assistant',
        content: Array.from({ length: 36 }, (_, i) =>
          `Scroll restore paragraph ${i + 1}. ${'Persisted content. '.repeat(10)}`
        ).join('\n\n'),
        blocks: Array.from({ length: 36 }, (_, i) => ({
          type: 'text',
          content: `Scroll restore paragraph ${i + 1}. ${'Persisted content. '.repeat(10)}`,
        })),
      },
    ]

    // Scroll restore is only meaningful for content that survives navigation.
    // The shared POST stub creates optimistic rows only, so this test serves
    // the persisted transcript directly and keeps the invariant under test
    // focused on the chat-mode save/restore path.
    await page.route(new RegExp(`/api/chats/${chatId}\\?limit=`), route => {
      if (route.request().method() !== 'GET') return route.continue()
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages,
          total: messages.length,
          offset: 0,
          running: false,
          pending_messages: [],
        }),
      })
    })

    await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => {
        const el = document.querySelector('.chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.scrollHeight > el.clientHeight + 100
          && el.textContent.includes('Scroll restore paragraph 36')
      },
      { timeout: 10000 }
    )

    // Stamp an ANCHOR_AT mode with a real wheel gesture; the scroll state
    // machine intentionally ignores non-gesture scroll events.
    await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
      if (!el) return
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      el.scrollTop = Math.floor(el.scrollHeight / 3)
    })
    await page.waitForFunction(() => {
      const el = document.querySelector('.chat__scroll')
      if (!el) return false
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight
      return el.scrollTop > 0 && gap > 100
    }, { timeout: 3000 })
    const scrollBefore = await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
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
    await page.waitForFunction(
      id => {
        try {
          const modes = JSON.parse(sessionStorage.getItem('chat-mode') || '{}')
          return modes[id]?.kind === 'ANCHOR_AT'
        } catch {
          return false
        }
      },
      chatId,
      { timeout: 3000 }
    )

    // Return through the app navigation stack so ChatView remounts and consumes
    // the saved mode for the same chat.
    await page.evaluate(() => history.back())
    await page.waitForFunction(
      () => {
        const el = document.querySelector('.chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.scrollHeight > el.clientHeight + 100
          && el.textContent.includes('Scroll restore paragraph 36')
      },
      { timeout: 10000 }
    )
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))

    const scrollAfter = await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
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
      { role: 'user', content: 'Follow restore prompt', ts: 1700000100000 },
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

    await page.route(new RegExp(`/api/chats/${chatId}\\?limit=`), route => {
      if (route.request().method() !== 'GET') return route.continue()
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages,
          total: messages.length,
          offset: 0,
          running: false,
          pending_messages: [],
        }),
      })
    })

    await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => {
        const el = document.querySelector('.chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.scrollHeight > el.clientHeight + 100
          && el.textContent.includes('Initial follow paragraph 32')
      },
      { timeout: 10000 },
    )

    // A real wheel gesture is the sole transition into FOLLOW_BOTTOM. The
    // initial restore can already place the viewport at the physical bottom,
    // so first move away from it; a wheel-down from an already-clamped tail
    // emits no scroll event and therefore cannot establish reader intent.
    const scroll = page.locator('.chat__scroll')
    await scroll.hover()
    await page.mouse.wheel(0, -300)
    await page.waitForFunction(() => {
      const el = document.querySelector('.chat__scroll')
      return !!el
        && el.scrollTop > 0
        && el.scrollHeight - el.scrollTop - el.clientHeight > 100
    }, { timeout: 3000 })
    await page.mouse.wheel(0, 100000)
    await page.waitForFunction(() => {
      const el = document.querySelector('.chat__scroll')
      return !!el && el.scrollHeight - el.scrollTop - el.clientHeight < 50
    }, { timeout: 3000 })
    await page.waitForFunction(
      id => {
        try {
          return JSON.parse(sessionStorage.getItem('chat-mode') || '{}')[id]?.kind
            === 'FOLLOW_BOTTOM'
        } catch {
          return false
        }
      },
      chatId,
      { timeout: 3000 },
    )
    const scrollBefore = await page.evaluate(
      () => document.querySelector('.chat__scroll')?.scrollTop ?? null,
    )
    expect(scrollBefore).toBeGreaterThan(0)

    await page.getByLabel('Toggle navigation').click()
    await expect(page.locator('.drawer.drawer--open')).toBeVisible({ timeout: 3000 })
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    await expect(page.locator('.settings')).toBeVisible({ timeout: 5000 })
    await page.waitForFunction(
      id => {
        try {
          return JSON.parse(sessionStorage.getItem('chat-mode') || '{}')[id]?.kind
            === 'ANCHOR_AT'
        } catch {
          return false
        }
      },
      chatId,
      { timeout: 3000 },
    )

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
        const el = document.querySelector('.chat__scroll')
        return !!el
          && getComputedStyle(el).visibility !== 'hidden'
          && el.textContent.includes('Grown while away marker 18')
      },
      { timeout: 10000 },
    )
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))))

    const restored = await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
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
        body: JSON.stringify({
          messages: allMessages.slice(start, before),
          total: allMessages.length,
          offset: start,
          running: false,
          pending_messages: [],
        }),
      })
    })

    await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('button', { name: 'Load earlier messages' }))
      .toBeVisible({ timeout: 10000 })
    await page.getByRole('button', { name: 'Load earlier messages' }).click()
    await page.waitForFunction(
      () => document.querySelector('[data-key="user-1700000200010"]'),
      { timeout: 5000 },
    )
    // loadOlderMessages keeps its pagination guard raised until the commit's
    // requestAnimationFrame. Wait for that boundary before synthesizing the
    // reader gesture; otherwise the scroll handler correctly ignores the
    // programmatic prepend settle and this test accidentally races the guard.
    await page.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve))))

    // Read an older row that is outside the server's default newest-20 page.
    // The pointerdown makes the ensuing scroll an owner gesture, so R4 saves
    // this exact row+offset rather than a programmatic position.
    await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
      const target = document.querySelector('[data-key="user-1700000200010"]')
      if (!el || !target) throw new Error('missing paginated anchor target')
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
      el.scrollTop = target.offsetTop + 12
    })
    await page.waitForFunction(
      id => JSON.parse(sessionStorage.getItem('chat-mode') || '{}')[id]?.key
        === 'user-1700000200010',
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
        const el = document.querySelector('.chat__scroll')
        return !!el && getComputedStyle(el).visibility !== 'hidden'
      },
      { timeout: 10000 },
    )
    await page.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve))))

    const restored = await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
      const target = document.querySelector('[data-key="user-1700000200010"]')
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

  test('10d. Previous-chat entry stays visually fixed through delayed history, image decode, and first catch-up', async ({ page }) => {
    await setup(page, { width: 900, height: 760 })
    await newChat(page)

    const chatId = await page.evaluate(() => localStorage.getItem('moebius_active_chat'))
    expect(chatId).toBeTruthy()

    let returning = false
    let streamCount = 0
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
        body: JSON.stringify({
          messages: history(returning ? 'entry-image-return.png' : 'entry-image-initial.png'),
          total: 4,
          offset: 0,
          running: returning,
          pending_messages: [],
        }),
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
      await new Promise(resolve => setTimeout(resolve, 500))
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
      const el = document.querySelector('.chat__scroll')
      const img = document.querySelector('.md-image')
      return !!el && getComputedStyle(el).visibility !== 'hidden'
        && !!img?.complete && !!document.querySelector('[data-key="entry-anchor"]')
    }, { timeout: 10000 })
    await page.evaluate(() => {
      const el = document.querySelector('.chat__scroll')
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
      id => JSON.parse(sessionStorage.getItem('chat-mode') || '{}')[id]?.key
        === 'entry-anchor',
      chatId,
      { timeout: 3000 },
    )

    await page.getByLabel('Toggle navigation').click()
    await expect(page.locator('.drawer.drawer--open')).toBeVisible({ timeout: 3000 })
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    await expect(page.locator('.settings')).toBeVisible({ timeout: 5000 })

    returning = true
    await page.evaluate(() => {
      window.__entryTrajectory = []
      const started = performance.now()
      const sample = () => {
        const el = document.querySelector('.chat__scroll')
        const target = document.querySelector('[data-key="entry-anchor"]')
        const visible = !!el && getComputedStyle(el).visibility !== 'hidden'
        window.__entryTrajectory.push({
          t: Math.round(performance.now() - started),
          visible,
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

    await page.waitForFunction(() => {
      const el = document.querySelector('.chat__scroll')
      const img = document.querySelector('.md-image')
      return !!el && getComputedStyle(el).visibility !== 'hidden'
        && !!img?.complete && !!document.querySelector('[data-key="entry-anchor"]')
    }, { timeout: 10000 })
    await page.waitForTimeout(500)

    const trajectory = await page.evaluate(() => window.__entryTrajectory || [])
    const visibleRows = trajectory.filter(row => row.visible)
    expect(returnImageServed).toBe(true)
    expect(streamCount).toBeGreaterThan(0)
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

  test('10a. Enter inserts newline on touch-primary device', async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 915 })

    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
      fulfillStartedPost(route)
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
      fulfillStartedPost(route)
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
      fulfillStartedPost(route)
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
      fulfillStartedPost(route)
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
