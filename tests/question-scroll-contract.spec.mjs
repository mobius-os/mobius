/**
 * Lock-in tests for the chat-redesign — the four user-visible bugs
 * the redesign exists to fix. If any of these regresses, fix it
 * before merging anything else into ChatView/useScrollMode.
 *
 *   1. AskUserQuestion answerable post-question.
 *   2. Mid-stream return — message visible, scroll lands on the
 *      reading anchor (not blank, not scrolled to top).
 *   3. Tool collapse during streaming — no snap to bottom.
 *   4. Auto-follow engages when user scrolls to bottom.
 *
 * Tests use mocked SSE / mocked /messages — no agent tokens spent.
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/chat-redesign.spec.mjs
 */
import { test, expect } from '@playwright/test'
import { createTaggedChat, attachCleanup } from './_chatTracker.mjs'
import { mockPendingQuestionState } from './_mockPendingQuestion.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Per-worker cleanup: see tests/_chatTracker.mjs.
attachCleanup()


function fulfillStartedPost(route) {
  if (route.request().method() !== 'POST') return route.continue()
  return route.fulfill({ status: 202, body: '{"status":"started"}' })
}


/** Helper: log in via the storageState set by auth.setup.mjs and
 *  install a default route mock that returns 204 for /stream. */
async function setupWithStreamMock(
  page,
  streamBody,
  viewport = { width: 412, height: 915 },
) {
  await page.setViewportSize(viewport)
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route =>
    fulfillStartedPost(route)
  )
  await page.route('**/api/chat/stop', route =>
    route.fulfill({ status: 200, body: '{}' })
  )
  if (streamBody) {
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
      route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: typeof streamBody === 'function' ? streamBody() : streamBody,
      })
    )
  } else {
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route =>
      route.fulfill({ status: 204, body: '' })
    )
  }
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
          || document.querySelector('.chat__scroll')
          || document.querySelector('.chat__form')),
    undefined, { timeout: 10000 }
  )
}


/** Navigate to a new empty chat. */
async function newChat(page) {
  // Create the chat via API first (so it's tagged with the worker
  // prefix and can be reaped after the spec finishes), then click
  // through the UI to actually land on it.
  const chat = await createTaggedChat(page)
  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chat.id)}`, {
    waitUntil: 'domcontentloaded',
  })
  await expect(page.locator('[data-chat-surface="painted"] .chat__empty-wrap')).toBeVisible({ timeout: 8000 })
}


async function sendMessage(page, text) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(text)
  await page.keyboard.press('Enter')
  // Wait for the optimistic user-message LI to render — the
  // deterministic signal that the send landed. The previous
  // strategy (waiting on `.chat__scroll` to be visible) raced the
  // hide-then-reveal safety cap when prior tests left state in the
  // shared storageState; downstream assertions already do their
  // own visibility waits, so blocking on container visibility up
  // front bought nothing.
  await expect(page.locator('[data-chat-surface="painted"] .chat__msg--user').first()).toBeVisible({ timeout: 8000 })
  await page.evaluate(() => new Promise(r =>
    requestAnimationFrame(() => requestAnimationFrame(r))
  ))
}


// ─────────────────────────────────────────────────────────────────
// BUG 1: AskUserQuestion answerable
// ─────────────────────────────────────────────────────────────────

// These tests mock the network via page.route and assert no service-worker
// behavior. The real SW claims the page ~1s after load and its fetch handler
// bypasses page.route, silently un-mocking the API/stream contracts mid-test
// (the app-canvas and steer-queued specs both hit this class). Block it so
// the mocks stay authoritative for the whole test.
test.use({ serviceWorkers: 'block' })

test.describe('Bug 1: AskUserQuestion', () => {













  test('multiline custom answers grow inline without moving the conversation', async ({ page }) => {
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'question',
        question_id: 'q-steady-multiline',
        questions: [{
          question: 'Describe the change',
          header: 'Details',
          multiSelect: false,
          options: [
            { label: 'Small', description: 'Keep the change focused' },
            { label: 'Broad', description: 'Cover the surrounding behavior' },
          ],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    await setupWithStreamMock(page, streamBody, { width: 426, height: 510 })
    await mockPendingQuestionState(page, 'q-steady-multiline')
    await newChat(page)
    await sendMessage(page, 'Ask for multiline details')

    const card = page.locator('[data-chat-surface="painted"] .qcard')
    const customAnswer = card.getByRole('textbox', {
      name: 'Custom answer for: Describe the change',
    })
    await expect(card).toBeVisible({ timeout: 5000 })
    expect(await card.evaluate(el => !!el.closest('.chat__scroll'))).toBe(true)
    expect(await card.evaluate(el => !!el.closest('.chat__question-dock'))).toBe(false)
    await customAnswer.focus()

    const geometry = () => card.evaluate(el => {
      const scroll = el.closest('.chat__scroll')
      const input = el.querySelector('.qcard__input')
      const rect = node => node?.getBoundingClientRect()
      return {
        cardTop: rect(el)?.top,
        cardHeight: rect(el)?.height,
        inputHeight: rect(input)?.height,
        chatScrollTop: scroll?.scrollTop,
      }
    })

    const before = await geometry()
    // Plain Enter now sends in the inline answer editor (matches the composer),
    // so a multi-line answer is built with Shift+Enter for each newline.
    await customAnswer.pressSequentially('First line')
    await page.keyboard.press('Shift+Enter')
    await customAnswer.pressSequentially('Second line')
    await page.keyboard.press('Shift+Enter')
    await customAnswer.pressSequentially('Third line')
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))
    const after = await geometry()

    await expect(customAnswer).toHaveValue('First line\nSecond line\nThird line')
    expect(after.cardHeight).toBeGreaterThan(before.cardHeight)
    expect(after.inputHeight).toBeGreaterThan(before.inputHeight)
    expect(after.cardTop).toBeCloseTo(before.cardTop, 5)
    expect(after.chatScrollTop).toBeCloseTo(before.chatScrollTop, 5)

    // Past the growth cap, the writing field—not the transcript—owns overflow.
    // Drive the real keyboard path so caret reveal, beforeinput, input, and the
    // chat scroll owner race exactly as they do for an owner writing an answer.
    for (let line = 4; line <= 14; line += 1) {
      await page.keyboard.press('Shift+Enter')
      await customAnswer.pressSequentially(`Line ${line}`)
    }
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))
    const capped = await geometry()
    const inputScrollTop = await customAnswer.evaluate(el => el.scrollTop)
    expect(capped.inputHeight).toBeLessThanOrEqual(181)
    expect(inputScrollTop).toBeGreaterThan(0)
    expect(capped.cardTop).toBeCloseTo(before.cardTop, 5)
    expect(capped.chatScrollTop).toBeCloseTo(before.chatScrollTop, 5)
  })


  test('a nested answer editor keeps its keys and wheel from relatching the transcript', async ({ page }) => {
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'text',
        content: 'Earlier context keeps the transcript scrollable. '.repeat(180),
      })}\n\n`,
      `data: ${JSON.stringify({
        type: 'question',
        question_id: 'q-nested-input-owner',
        questions: [{
          question: 'Write the detailed answer',
          header: 'Details',
          multiSelect: false,
          options: [{ label: 'Skip writing' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    await setupWithStreamMock(page, streamBody, { width: 426, height: 510 })
    await mockPendingQuestionState(page, 'q-nested-input-owner')
    await newChat(page)
    await sendMessage(page, 'Ask for a detailed answer')

    const scroll = page.locator('[data-chat-surface="painted"] .chat__scroll')
    const customAnswer = page.getByRole('textbox', {
      name: 'Custom answer for: Write the detailed answer',
    })
    await expect(customAnswer).toBeVisible({ timeout: 5000 })
    await customAnswer.fill(
      Array.from({ length: 24 }, (_, index) => `Detail line ${index + 1}`).join('\n'),
    )
    await expect.poll(() => customAnswer.evaluate(
      el => el.scrollHeight - el.clientHeight,
    )).toBeGreaterThan(20)

    // Establish a genuine reader hold, then move its DOM coordinate to the
    // physical clamp without granting follow. This is the exact state in which
    // a bubbled End/wheel used to relatch the outer transcript.
    await scroll.evaluate(el => {
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0 }))
      el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 160)
      el.dispatchEvent(new Event('scroll'))
      el.dispatchEvent(new Event('scrollend'))
    })
    await expect(scroll).toHaveAttribute('data-scroll-mode', 'ANCHOR_AT')
    await scroll.evaluate(el => { el.scrollTop = el.scrollHeight })
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))
    await expect(scroll).toHaveAttribute('data-scroll-mode', 'ANCHOR_AT')

    await customAnswer.focus()
    await customAnswer.press('End')
    await expect(scroll).toHaveAttribute('data-scroll-mode', 'ANCHOR_AT')

    await customAnswer.evaluate(el => { el.scrollTop = 0 })
    const outerBefore = await scroll.evaluate(el => el.scrollTop)
    await customAnswer.hover()
    await page.mouse.wheel(0, 120)
    await expect.poll(() => customAnswer.evaluate(el => el.scrollTop))
      .toBeGreaterThan(0)
    await expect(scroll).toHaveAttribute('data-scroll-mode', 'ANCHOR_AT')
    const outerAfter = await scroll.evaluate(el => el.scrollTop)
    expect(Math.abs(outerAfter - outerBefore)).toBeLessThanOrEqual(1)
  })









})


// ─────────────────────────────────────────────────────────────────
// BUG 3: Mid-stream return — DB partial bridge
// ─────────────────────────────────────────────────────────────────

test.describe('Bug 3: mid-stream return shows persisted content', () => {

  // The full mid-stream-return scenario is hard to reproduce
  // hermetically (requires precise SSE timing + a real backend). What
  // we CAN lock in is the smaller invariant: the ChatView no longer
  // strips a kept DB partial from `messages` on mount when
  // `data.running=true`. We verify by exercising the actual code
  // path via injected DOM (the same pattern existing spacer tests
  // use) and asserting the assistant message stays rendered when
  // streamItems is empty.

})


// ─────────────────────────────────────────────────────────────────
// BUG 2/4: scroll behaviors — geometry-owned state machine
// ─────────────────────────────────────────────────────────────────

test.describe('Bug 2/4: scroll state machine', () => {








})


// ─────────────────────────────────────────────────────────────────
// Q&A backend race — answers persist atomically
// ─────────────────────────────────────────────────────────────────

test.describe('Q&A atomic write', () => {



  test('an Android viewport growth keeps a submitted question anchored', async ({ page }) => {
    const longLead = 'Context before the question. '.repeat(180)
    const streamBody = [
      `data: ${JSON.stringify({ type: 'text', content: longLead })}\n\n`,
      `data: ${JSON.stringify({
        type: 'question',
        question_id: 'q-viewport-anchor',
        questions: [{
          question: 'Keep this card still?',
          header: 'Position',
          multiSelect: false,
          options: [{ label: 'Yes' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    let streamCount = 0
    let releaseAnswer
    let markAnswerStarted
    const answerStarted = new Promise(resolve => { markAnswerStarted = resolve })
    let pendingQuestion

    await page.setViewportSize({ width: 426, height: 860 })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
      if (route.request().method() !== 'POST') return route.continue()
      const body = route.request().postDataJSON()
      if (!body.answers) return fulfillStartedPost(route)
      markAnswerStarted()
      await new Promise(resolve => { releaseAnswer = resolve })
      pendingQuestion.markAnswered()
      return route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'answer_delivered', answer_turn: 'same' }),
      })
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: streamCount++ === 0 ? streamBody : 'data: {"type":"done"}\n\n',
      })
    })
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
    pendingQuestion = await mockPendingQuestionState(page, 'q-viewport-anchor')
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
            || document.querySelector('[data-chat-surface="painted"] .chat__form')),
      undefined, { timeout: 10000 },
    )
    await newChat(page)
    await sendMessage(page, 'Ask the anchored question')

    const card = page.locator('[data-chat-surface="painted"] .qcard')
    await expect(card).toBeVisible({ timeout: 5000 })
    await page.evaluate(() => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      if (scroll) scroll.scrollTop = scroll.scrollHeight
    })
    await page.locator('[data-chat-surface="painted"] .qcard__opt', { hasText: 'Yes' }).click()

    const submit = page.locator('[data-chat-surface="painted"] .qcard__submit')
    const submitClick = submit.click()
    await answerStarted
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))

    const geometry = () => page.evaluate(() => {
      const scroll = document.querySelector('[data-chat-surface="painted"] .chat__scroll')
      const question = document.querySelector('[data-chat-surface="painted"] .qcard')
      const spacer = document.querySelector('[data-chat-surface="painted"] .spacer-dynamic')
      const sr = scroll?.getBoundingClientRect()
      const qr = question?.getBoundingClientRect()
      return {
        scrollTop: scroll?.scrollTop ?? null,
        cardTop: sr && qr ? qr.top - sr.top : null,
        viewport: scroll?.clientHeight ?? null,
        spacer: spacer?.offsetHeight ?? null,
      }
    })

    const before = await geometry()
    await page.setViewportSize({ width: 426, height: 960 })
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))
    const after = await geometry()
    const modeAfterResize = await page.evaluate(() => (
      document.querySelector('[data-chat-surface="painted"] .chat__scroll')
        ?.dataset.scrollMode || null
    ))
    releaseAnswer()
    await submitClick

    expect(after.viewport).toBeGreaterThan(before.viewport)
    expect(modeAfterResize).toBe('ANCHOR_AT')
    expect(after.cardTop).toBeCloseTo(before.cardTop, 0)
  })
})


// ─────────────────────────────────────────────────────────────────
// Error-block persistence — locks in the be32e58 fix
// ─────────────────────────────────────────────────────────────────
//
// Two-stage assertion: the error renders during streaming AND
// survives a chat reload. The earlier shape mismatch (streaming
// pushed a text block, backend persisted an error block, frontend
// had no error-render branch) silently dropped the error on
// chat return.

test.describe('Error block: persists across chat return', () => {

  test('streamed `error` event renders as a system notice and stays after reload', async ({ page }) => {
    const streamBody = [
      'data: {"type":"text","content":"Working on it..."}\n\n',
      'data: {"type":"error","message":"Quota exceeded. Try again later."}\n\n',
      'data: {"type":"done"}\n\n',
    ].join('')

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
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: streamBody,
      })
    )

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
            || document.querySelector('[data-chat-surface="painted"] .chat__form')),
      undefined, { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Try something')

    // The error notice appears during streaming with the
    // system-notice class — distinct from the assistant bubble.
    const errorBlock = page.locator('[data-chat-surface="painted"] .chat__text--error', {
      hasText: 'Quota exceeded',
    })
    await expect(errorBlock).toBeVisible({ timeout: 5000 })
    await expect(page.locator('[data-chat-surface="painted"] .chat__error-label', { hasText: /Error/i }))
      .toBeVisible()

    // Wait for the stream's `done` to fire and promote the
    // streamItems into a persisted assistant `<li>`. The
    // promote replaces the live streaming list with one built
    // from the assistant message's `blocks` array — that's
    // exactly the path the bug fix targeted (MsgContent's new
    // `block.type === 'error'` branch). If the branch is
    // missing, the error block on the promoted message renders
    // to null and disappears here.
    await page.waitForFunction(
      () => !document.querySelector('[data-chat-surface="painted"] .chat__stop'),
      undefined, { timeout: 5000 },
    )
    // The Stop button is gone; the streaming `<li>` (which
    // shares its rendering path with the streaming render
    // branch in ChatView.jsx) is replaced by the assistant
    // `<li>` whose body comes from MsgContent.
    await expect(
      page.locator('[data-chat-surface="painted"] .chat__text--error', { hasText: 'Quota exceeded' }),
    ).toBeVisible({ timeout: 3000 })
  })

  test('URLs in error messages render as clickable links', async ({ page }) => {
    // Provider error payloads typically include billing / upgrade
    // links ("Upgrade to Pro (https://chatgpt.com/explore/pro)").
    // Routing error.message through StandardMarkdown auto-links
    // them so the user can tap straight from the chat instead of
    // copy-pasting. Before this fix the URL rendered as plain
    // text.
    const errorMsg = 'Quota exceeded. Upgrade at https://example.test/billing'
    const streamBody = [
      `data: ${JSON.stringify({ type: 'error', message: errorMsg })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')

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
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: streamBody,
      })
    )

    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('[data-chat-surface="painted"] .chat__empty-wrap')
            || document.querySelector('[data-chat-surface="painted"] .chat__form')),
      undefined, { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Trigger error')

    // The error renders as a system notice. The URL inside it
    // must be an actual <a href> — assert the anchor exists with
    // the URL the message contained.
    const link = page.locator('[data-chat-surface="painted"] .chat__text--error a[href*="example.test/billing"]')
    await expect(link).toBeVisible({ timeout: 5000 })
  })
})
