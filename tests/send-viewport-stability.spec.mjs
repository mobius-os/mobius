import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
// Hold the acknowledgement beyond the keyboard-close transition so the test
// proves pre-stream viewport stability rather than relying on response timing.
const SECOND_ACK_DELAY_MS = 300

attachCleanup()
test.use({ serviceWorkers: 'block' })

async function installStreams(page) {
  await page.addInitScript(() => {
    const realFetch = window.fetch.bind(window)
    let streamIndex = 0
    const streams = [
      [
        [0, { type: 'catch_up_done' }],
        [30, { type: 'text', content: 'First response paragraph. '.repeat(160) }],
        [60, { type: 'done' }],
      ],
      [
        // Keep the entire landing window free of assistant output.
        [1500, { type: 'catch_up_done' }],
        [1800, { type: 'text', content: 'SECOND_STREAM_STARTED' }],
        [2000, { type: 'done' }],
      ],
    ]
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      if (!/\/api\/chats\/[^/]+\/stream$/.test(url)) {
        return realFetch(input, init)
      }
      const events = streams[streamIndex++] || []
      const encoder = new TextEncoder()
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          for (const [delay, event] of events) {
            setTimeout(() => {
              controller.enqueue(encoder.encode(
                `data: ${JSON.stringify(event)}\n\n`,
              ))
              if (event.type === 'done') controller.close()
            }, delay)
          }
        },
      }), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }))
    }
  })
}

async function send(page, text) {
  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
}

test('keyboard close never paints a sent row below its pin', async ({ page }) => {
  let running = false
  let sendCount = 0
  let serverMessages = []
  await page.setViewportSize({ width: 412, height: 915 })
  await installStreams(page)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
    const request = route.request().postDataJSON()
    sendCount += 1
    running = true
    setTimeout(() => { running = false }, sendCount === 1 ? 120 : 2500)
    const message = {
      role: 'user',
      content: request.content,
      blocks: [{ type: 'text', content: request.content }],
      ts: 1700000800000 + sendCount * 1000,
      cid: request.cid,
    }
    serverMessages = [...serverMessages, message]
    if (sendCount === 2 && SECOND_ACK_DELAY_MS > 0) {
      await new Promise(resolve => setTimeout(resolve, SECOND_ACK_DELAY_MS))
    }
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'started', message }),
    })
  })
  await page.route('**/api/chat/stop', route => (
    route.fulfill({ status: 200, body: '{}' })
  ))

  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'send-landing-trace')
  expect(chat?.id).toBeTruthy()
  await page.route(new RegExp(`/api/chats/${chat.id}/runtime(?:\\?.*)?$`), route => (
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        running,
        active_goal_objective: null,
        pending_messages: [],
        pending_question_id: null,
      }),
    })
  ))
  await page.route(new RegExp(`/api/chats/${chat.id}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: chat.id,
        messages: serverMessages,
        total: serverMessages.length,
        offset: 0,
        running,
        pending_messages: [],
        pending_question_id: null,
        provider: 'codex',
        agent_settings_json: { model: 'claude-sonnet-4-6' },
      }),
    })
  })
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })

  await send(page, 'First user message')
  await expect(page.locator(
    '[data-chat-surface="painted"] .chat__msg--assistant',
  )).toContainText('First response paragraph.', { timeout: 5000 })
  await page.waitForFunction(() => (
    !document.querySelector('[data-chat-surface="painted"] .chat__stop')
  ), undefined, { timeout: 5000 })
  await page.waitForTimeout(300)
  serverMessages = [
    serverMessages[0],
    {
      role: 'assistant',
      content: 'First response paragraph. '.repeat(160),
      blocks: [{ type: 'text', content: 'First response paragraph. '.repeat(160) }],
      ts: 1700000801000,
    },
  ]

  const surface = page.locator('[data-chat-surface="painted"]')
  const input = surface.getByRole('textbox', { name: 'Message Möbius…' })
  // Mirror a phone composer with its software keyboard open. The shell's
  // scroll viewport shrinks before the send and grows again after blur.
  await page.setViewportSize({ width: 412, height: 600 })
  await page.waitForTimeout(150)
  const multiline = [
    'Second user message with a longer report about the send landing.',
    'It is deliberately tall enough to exercise the real composer collapse.',
    'The response remains delayed while every painted frame is sampled.',
  ].join('\n')
  await input.fill(multiline)
  await expect(surface.locator('.chat__pill')).toHaveClass(/chat__pill--tall/)
  // Composer growth changes footer geometry. Re-enter the physical tail just
  // before submit, matching the reported precondition.
  await page.evaluate(() => {
    const scroll = document.querySelector(
      '[data-chat-surface="painted"] .chat__scroll',
    )
    scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    scroll.scrollTop = Math.max(0, scroll.scrollTop - 1)
    scroll.scrollTop = scroll.scrollHeight
  })

  await page.evaluate(() => {
    const scroll = document.querySelector(
      '[data-chat-surface="painted"] .chat__scroll',
    )
    scroll.scrollTop = scroll.scrollHeight
  })
  await page.waitForTimeout(150)
  await page.evaluate(() => {
    const scroll = document.querySelector(
      '[data-chat-surface="painted"] .chat__scroll',
    )
    scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    scroll.scrollTop = Math.max(0, scroll.scrollTop - 1)
    scroll.scrollTop = scroll.scrollHeight
  })

  await page.evaluate(() => {
    window.__sendLandingFrames = []
    window.__sendLandingSampling = true
    const startedAt = performance.now()
    const sample = () => {
      const surface = document.querySelector('[data-chat-surface="painted"]')
      const scroll = surface?.querySelector('.chat__scroll')
      const users = surface?.querySelectorAll('.chat__msg--user') || []
      const row = users[users.length - 1]
      const scrollRect = scroll?.getBoundingClientRect()
      const rowRect = row?.getBoundingClientRect()
      const spacer = surface?.querySelector('.spacer-dynamic')
      window.__sendLandingFrames.push({
        t: Math.round(performance.now() - startedAt),
        users: users.length,
        top: rowRect && scrollRect ? Math.round(rowRect.top - scrollRect.top) : null,
        scrollTop: Math.round(scroll?.scrollTop || 0),
        scrollHeight: Math.round(scroll?.scrollHeight || 0),
        spacer: Math.round(spacer?.offsetHeight || 0),
        mode: scroll?.dataset.scrollMode || null,
        jumpToLatest: !!surface?.querySelector('.chat__jump-latest'),
      })
      if (window.__sendLandingSampling) requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  })

  await page.keyboard.press('Enter')
  await page.waitForTimeout(16)
  await page.setViewportSize({ width: 412, height: 915 })
  await page.waitForTimeout(1200)
  const evidence = await page.evaluate(() => {
    window.__sendLandingSampling = false
    const frames = window.__sendLandingFrames || []
    const visible = frames.filter(frame => frame.users >= 2 && frame.top != null)
    const changes = visible.filter((frame, index) => {
      if (index === 0) return true
      const previous = visible[index - 1]
      return frame.top !== previous.top
        || frame.mode !== previous.mode
        || frame.spacer !== previous.spacer
        || frame.scrollTop !== previous.scrollTop
    })
    return {
      changes,
      maxTop: visible.length ? Math.max(...visible.map(frame => frame.top)) : null,
      sawJumpToLatest: visible.some(frame => frame.jumpToLatest),
      trace: window.__mobiusChatScrollTrace || null,
    }
  })
  expect(evidence.changes.length).toBeGreaterThan(0)
  expect(
    evidence.maxTop,
    `painted send movement: ${JSON.stringify(evidence)}`,
  ).toBeLessThanOrEqual(16)
  expect(
    evidence.sawJumpToLatest,
    `the transient pin reserve must not advertise a reader escape: ${JSON.stringify(evidence)}`,
  ).toBe(false)
})
