/**
 * Browser contract R5c: a newly pending owner decision comes into view once.
 *
 * A reader holding earlier history used to get only a small offscreen nudge,
 * and missed decisions. The chat now reveals the card from its top once per
 * decision — never underneath a resting finger, never out of live follow, and
 * never again after the reader scrolls away. Every request is intercepted; the
 * fixture cannot mutate a real chat.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/question-arrival-reveal.spec.mjs
 */
import { test, expect, serveRecoveryBuild } from './_recoveryBrowser.mjs'
import {
  installMockAgentProvider,
  mockDeliveryReady,
  runtimeSnapshot,
  testChatAgentSettings,
} from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const SCROLL = '[data-chat-surface="painted"] .chat__scroll'
const CARD = '[data-chat-surface="painted"] .qcard'
const NUDGE = '.chat__question-nudge'

test.use({ serviceWorkers: 'block' })

const DECISION = {
  type: 'question',
  question_id: 'q-arrival-reveal',
  questions: [{
    question: 'Run the cutover tonight?',
    header: 'Decision',
    multiSelect: false,
    options: [
      { label: 'Tonight', description: 'Run the cutover this evening.' },
      { label: 'Tomorrow', description: 'Hold it until the morning.' },
    ],
  }],
}
const LIVE_PREFIX = 'Checking the cutover window before asking.'

const history = [
  {
    role: 'user',
    content: 'Walk me through the migration plan.',
    cid: 'arrival-user-1',
    ts: 1788800000000,
    blocks: [{ type: 'text', content: 'Walk me through the migration plan.' }],
  },
  {
    role: 'assistant',
    id: 'arrival-assistant-1',
    ts: 1788800000100,
    content: '',
    blocks: Array.from({ length: 40 }, (_, i) => ({
      type: 'text',
      content: `Plan step ${i + 1}. ${'Enough history to hold an earlier reading position. '.repeat(7)}`,
    })),
  },
  {
    role: 'user',
    content: 'Plan the cutover next.',
    cid: 'arrival-user-2',
    ts: 1788800000200,
    blocks: [{ type: 'text', content: 'Plan the cutover next.' }],
  },
]
const durableDecisionRow = {
  role: 'assistant',
  id: 'arrival-assistant-2',
  ts: 1788800000300,
  content: '',
  blocks: [{ type: 'text', content: LIVE_PREFIX }, DECISION],
}

/** One chat whose server state the test advances explicitly. A live turn
 * streams a short reply; `publishDecision` delivers the card exactly as the
 * server's broadcast would, and makes it durable at the same moment. */
async function installDecisionChat(page, { liveTurn = true, decisionPending = false } = {}) {
  const chatId = crypto.randomUUID()
  const state = { running: liveTurn, pendingQuestionId: decisionPending ? DECISION.question_id : null }

  await serveRecoveryBuild(page)
  await installMockAgentProvider(page)
  await page.route('**/api/**', route => route.request().method() === 'GET'
    ? route.fallback()
    : route.fulfill({ status: 501, json: { detail: 'Unexpected fixture mutation' } }))
  await mockDeliveryReady(page)
  await page.route(/\/api\/chats(?:\?.*)?$/, route => route.request().method() === 'GET'
    ? route.fulfill({ json: [{ id: chatId, title: 'Decision arrival fixture', has_messages: true, running: state.running }] })
    : route.fallback())
  await page.route(/\/api\/apps\/?(?:\?.*)?$/, route => route.fulfill({ json: [] }))
  const runtime = () => ({
    ...runtimeSnapshot({ running: state.running, pending_question_id: state.pendingQuestionId }),
    active_goal_objective: null,
    updated_at: null,
  })
  await page.route(new RegExp(`/api/chats/${chatId}/runtime(?:\\?.*)?$`), route => (
    route.request().method() === 'GET'
      ? route.fulfill({ json: runtime() })
      : route.fallback()
  ))
  await page.route(new RegExp(`/api/chats/${chatId}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const messages = state.pendingQuestionId ? [...history, durableDecisionRow] : history
    return route.fulfill({
      json: {
        ...runtime(),
        id: chatId,
        messages,
        total: messages.length,
        offset: 0,
        provider: 'claude',
        ...testChatAgentSettings(),
      },
    })
  })

  await page.addInitScript(({ chatId, prefix }) => {
    const realFetch = window.fetch.bind(window)
    const subscribers = new Set()
    window.__publishDecision = decision => {
      for (const emit of subscribers) emit(decision)
    }
    window.fetch = (input, init) => {
      const url = new URL(String(input?.url || input), location.href)
      if (url.pathname !== `/api/chats/${chatId}/stream`) return realFetch(input, init)
      const signal = init?.signal || input?.signal
      if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
      const encoder = new TextEncoder()
      let emit
      let abort
      const cleanup = () => {
        subscribers.delete(emit)
        signal?.removeEventListener('abort', abort)
      }
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          emit = event => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
          abort = () => {
            cleanup()
            controller.error(new DOMException('Aborted', 'AbortError'))
          }
          subscribers.add(emit)
          signal?.addEventListener('abort', abort, { once: true })
          emit({ type: 'stream_snapshot', items: [{ type: 'text', content: prefix }] })
          emit({ type: 'catch_up_done' })
        },
        cancel: cleanup,
      }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { chatId, prefix: LIVE_PREFIX })

  await page.setViewportSize({ width: 1512, height: 861 })
  return {
    chatId,
    async open() {
      await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chatId)}`, {
        waitUntil: 'domcontentloaded',
      })
      await expect(page.locator(SCROLL)).toBeVisible({ timeout: 15000 })
      await page.waitForFunction(selector => {
        const scroll = document.querySelector(selector)
        return !!scroll && scroll.scrollHeight > scroll.clientHeight + 2000
      }, SCROLL, { timeout: 10000 })
    },
    async publishDecision() {
      state.pendingQuestionId = DECISION.question_id
      await page.evaluate(decision => window.__publishDecision(decision), DECISION)
    },
    makeDecisionDurable() {
      state.running = false
      state.pendingQuestionId = DECISION.question_id
    },
  }
}

/** Move like a reader: an input opens the gesture, its scroll lands, and the
 * controller settles that position after the quiet edge. */
async function readerScrollsAboveTail(page, distance) {
  const settledBefore = await settledGestureCount(page)
  await page.evaluate(({ selector, distance }) => {
    const scroll = document.querySelector(selector)
    scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    scroll.scrollTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight - distance)
  }, { selector: SCROLL, distance })
  await expect.poll(() => settledGestureCount(page)).toBeGreaterThan(settledBefore)
}

/** Reach the physical tail the way a reader asks to follow: a scroll that
 * lands there, or an end-directed wheel when the view is already clamped. */
async function readerFollowsTail(page) {
  await page.evaluate(selector => {
    const scroll = document.querySelector(selector)
    const before = scroll.scrollTop
    scroll.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    scroll.scrollTop = scroll.scrollHeight
    if (Math.abs(scroll.scrollTop - before) < 1) {
      scroll.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 120 }))
    }
  }, SCROLL)
}

function settledGestureCount(page) {
  return page.evaluate(() => (window.__mobiusChatScrollTrace?.events || [])
    .filter(entry => entry.event === 'reader:scroll-settled').length)
}

/** Where the card sits relative to the part of the transcript the reader can
 * actually see: below the scroll box top and above the overlaid composer. */
function decisionGeometry(page) {
  return page.evaluate(({ scrollSelector, cardSelector }) => {
    const scroll = document.querySelector(scrollSelector)
    const card = document.querySelector(cardSelector)
    const foot = document.querySelector('[data-chat-surface="painted"] .chat__foot')
    const scrollRect = scroll?.getBoundingClientRect()
    const cardRect = card?.getBoundingClientRect()
    const usableBottom = Math.min(scrollRect?.bottom ?? 0, foot?.getBoundingClientRect().top ?? Infinity)
    return {
      mode: scroll?.dataset.scrollMode ?? null,
      scrollTop: scroll?.scrollTop ?? null,
      cardTopInView: cardRect && scrollRect
        ? cardRect.top >= scrollRect.top - 1 && cardRect.top < usableBottom - 48
        : false,
      cardOffscreen: cardRect && scrollRect
        ? cardRect.top >= usableBottom || cardRect.bottom <= scrollRect.top
        : true,
    }
  }, { scrollSelector: SCROLL, cardSelector: CARD })
}

test('a decision that arrives while the reader holds earlier history comes into view once', async ({ page }) => {
  const chat = await installDecisionChat(page)
  await chat.open()
  await readerScrollsAboveTail(page, 2400)
  const held = await decisionGeometry(page)
  expect(held.mode).toBe('ANCHOR_AT')

  await chat.publishDecision()
  await expect(page.locator(CARD)).toBeVisible({ timeout: 5000 })
  await expect.poll(async () => (await decisionGeometry(page)).cardTopInView).toBe(true)
  const revealed = await decisionGeometry(page)
  expect(revealed.mode).toBe('ANCHOR_AT')
  await expect(page.locator(NUDGE)).toHaveCount(0)

  // Scrolling away afterward is the reader's choice and is never undone; the
  // nudge becomes the way back.
  await readerScrollsAboveTail(page, 2400)
  const away = await decisionGeometry(page)
  expect(away.cardOffscreen).toBe(true)
  await expect(page.locator(NUDGE)).toBeVisible({ timeout: 5000 })
  await page.waitForTimeout(600)
  const later = await decisionGeometry(page)
  expect(later.scrollTop).toBe(away.scrollTop)
  expect(later.cardOffscreen).toBe(true)
})

test('a resting finger postpones the decision reveal until it lifts', async ({ page }) => {
  const chat = await installDecisionChat(page)
  await chat.open()
  await readerScrollsAboveTail(page, 2400)

  const box = await page.locator(SCROLL).boundingBox()
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
  const finger = [{ x: box.x + box.width / 2, y: box.y + 200, radiusX: 4, radiusY: 4, force: 1, id: 1 }]
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: finger })

  const beforeDecision = await decisionGeometry(page)
  await chat.publishDecision()
  await expect(page.locator(CARD)).toHaveCount(1, { timeout: 5000 })
  await page.waitForTimeout(700)
  const underFinger = await decisionGeometry(page)
  expect(underFinger.scrollTop).toBe(beforeDecision.scrollTop)
  expect(underFinger.cardOffscreen).toBe(true)

  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  await cdp.detach()
  await expect.poll(async () => (await decisionGeometry(page)).cardTopInView).toBe(true)
})

test('a followed tail keeps live follow when a decision arrives', async ({ page }) => {
  const chat = await installDecisionChat(page)
  await chat.open()
  await readerFollowsTail(page)
  await expect.poll(async () => (await decisionGeometry(page)).mode).toBe('FOLLOW_BOTTOM')

  await chat.publishDecision()
  await expect(page.locator(CARD)).toBeVisible({ timeout: 5000 })
  await expect.poll(async () => (await decisionGeometry(page)).cardTopInView).toBe(true)
  // Follow must survive so the answer's response keeps streaming into view.
  expect((await decisionGeometry(page)).mode).toBe('FOLLOW_BOTTOM')
})

test('opening a chat that is already waiting on a decision lands on it instead of the old reading position', async ({ page }) => {
  const chat = await installDecisionChat(page, { liveTurn: false })
  await chat.open()
  await readerScrollsAboveTail(page, 2400)

  // The decision is posted while the owner is elsewhere; the next visit is a
  // fresh page session that restores the saved earlier position.
  chat.makeDecisionDurable()
  await page.reload({ waitUntil: 'domcontentloaded' })
  await expect(page.locator(CARD)).toBeVisible({ timeout: 15000 })
  await expect.poll(async () => (await decisionGeometry(page)).cardTopInView).toBe(true)
  expect((await decisionGeometry(page)).mode).toBe('ANCHOR_AT')
})
