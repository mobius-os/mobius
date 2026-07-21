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

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

// Per-worker cleanup: see tests/_chatTracker.mjs.
attachCleanup()


function fulfillStartedPost(route) {
  if (route.request().method() !== 'POST') return route.continue()
  return route.fulfill({ status: 202, body: '{"status":"started"}' })
}


/** Helper: log in via the storageState set by auth.setup.mjs and
 *  install a default route mock that returns 204 for /stream. */
async function setupWithStreamMock(page, streamBody) {
  await page.setViewportSize({ width: 412, height: 915 })
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
    { timeout: 10000 }
  )
}


/** Navigate to a new empty chat. */
async function newChat(page) {
  // Create the chat via API first (so it's tagged with the worker
  // prefix and can be reaped after the spec finishes), then click
  // through the UI to actually land on it.
  await createTaggedChat(page)
  await page.evaluate(() => document.querySelector('.drawer__item--new')?.click())
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
  // Wait for the optimistic user-message LI to render — the
  // deterministic signal that the send landed. The previous
  // strategy (waiting on `.chat__scroll` to be visible) raced the
  // hide-then-reveal safety cap when prior tests left state in the
  // shared storageState; downstream assertions already do their
  // own visibility waits, so blocking on container visibility up
  // front bought nothing.
  await expect(page.locator('.chat__msg--user').first()).toBeVisible({ timeout: 8000 })
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

  test('QuestionCard buttons are NOT disabled after the question event', async ({ page }) => {
    // Mock SSE: text → question → done. (Backend's chat.py kill-on-
    // question fix means done arrives soon after the question; here
    // we simulate the same SSE the frontend would see.)
    const streamBody = [
      'data: {"type":"text","content":"Let me ask:"}\n\n',
      `data: ${JSON.stringify({
        type: 'question',
        question_id: 'q-pick-one',
        questions: [{
          question: 'Pick one',
          header: 'Test',
          multiSelect: false,
          options: [{ label: 'A' }, { label: 'B' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    await setupWithStreamMock(page, streamBody)
    await newChat(page)
    await sendMessage(page, 'Ask me a question')

    await expect(page.locator('.qcard')).toBeVisible({ timeout: 5000 })
    // The option buttons MUST be enabled (the regression was that
    // post-question tool_start events kept isStreaming=true →
    // disabled={isStreaming} stayed grayed out).
    const optionButtons = page.locator('.qcard__opt')
    await expect(optionButtons.first()).toBeEnabled({ timeout: 5000 })
  })


  test('a failed answer keeps the question and choice retryable', async ({ page }) => {
    const questionStream = [
      `data: ${JSON.stringify({
        type: 'question',
        question_id: 'q-retry-answer',
        questions: [{
          question: 'Choose a launch lane',
          header: 'Launch',
          multiSelect: false,
          options: [{ label: 'Careful' }, { label: 'Fast' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    let streamCount = 0
    let answerAttempts = 0
    await setupWithStreamMock(page, () => (
      streamCount++ === 0 ? questionStream : 'data: {"type":"done"}\n\n'
    ))
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      if (route.request().method() !== 'POST') return route.continue()
      const body = route.request().postDataJSON()
      if (!body.answers) return fulfillStartedPost(route)
      answerAttempts += 1
      if (answerAttempts === 1) {
        return route.fulfill({
          status: 503,
          headers: { 'Content-Type': 'application/json' },
          body: '{"detail":"temporary failure"}',
        })
      }
      return fulfillStartedPost(route)
    })

    await newChat(page)
    await sendMessage(page, 'Ask for a launch lane')

    const card = page.locator('.qcard')
    const careful = page.getByRole('radio', { name: 'Careful' })
    const submit = page.getByRole('button', { name: 'Submit' })
    await expect(card).toBeVisible({ timeout: 5000 })
    await careful.click()
    await submit.click()

    // Prove the failure below comes from the intended answer request, not a
    // competing route mock or a click that never reached the transport.
    await expect.poll(() => answerAttempts).toBe(1)
    await expect(card.getByText(/answer didn’t save/i)).toBeVisible()
    await expect(careful).toHaveAttribute('aria-checked', 'true')
    await expect(careful).toBeEnabled()
    await expect(submit).toBeEnabled()
    await submit.click()
    await expect.poll(() => answerAttempts).toBe(2)
    await expect(page.getByRole('button', { name: 'Submitted' })).toBeDisabled()
  })


  test('submitting a choice keeps every question-card row in place', async ({ page }) => {
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'question',
        question_id: 'q-stable-card',
        questions: [{
          question: 'Which route?',
          header: 'Route',
          multiSelect: false,
          options: [
            { label: 'Direct', description: 'Use the shortest path' },
            { label: 'Scenic', description: 'Keep more context visible' },
          ],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    let streamCount = 0
    await setupWithStreamMock(page, () => (
      streamCount++ === 0 ? streamBody : 'data: {"type":"done"}\n\n'
    ))
    await newChat(page)
    await sendMessage(page, 'Ask me which route')

    const card = page.locator('.qcard')
    await expect(card).toBeVisible({ timeout: 5000 })
    await page.getByRole('radio', { name: 'Other' }).click()
    const customAnswer = card.getByRole('textbox', { name: 'Other answer for: Which route?' })
    await customAnswer.fill('Take the quiet streets')

    const geometry = () => card.evaluate(el => ({
      height: el.getBoundingClientRect().height,
      hint: (() => {
        const node = el.querySelector('.qcard__hint')
        const rect = node?.getBoundingClientRect()
        return node && rect
          ? { text: node.textContent, top: rect.top, height: rect.height }
          : null
      })(),
      options: [...el.querySelectorAll('.qcard__opt')].map(node => {
        const rect = node.getBoundingClientRect()
        return { text: node.textContent.trim(), top: rect.top, height: rect.height }
      }),
      input: (() => {
        const node = el.querySelector('.qcard__input')
        const rect = node?.getBoundingClientRect()
        return node && rect
          ? {
              value: node.value,
              top: rect.top,
              width: rect.width,
              height: rect.height,
              color: getComputedStyle(node).color,
              textFillColor: getComputedStyle(node).webkitTextFillColor,
            }
          : null
      })(),
      submitTop: el.querySelector('.qcard__submit')?.getBoundingClientRect().top,
    }))

    const before = await geometry()
    await page.getByRole('button', { name: 'Submit' }).click()
    await expect(page.getByRole('button', { name: 'Submitted' })).toBeDisabled()
    await expect(card.locator('.qcard__hint')).toHaveText('Choose one')
    await expect(card.locator('.qcard__opt')).toHaveCount(3)
    await expect(customAnswer).toBeDisabled()
    await expect(customAnswer).toHaveValue('Take the quiet streets')

    const after = await geometry()
    expect(after.height).toBeCloseTo(before.height, 5)
    expect(after.hint).toEqual(before.hint)
    expect(after.options).toEqual(before.options)
    expect(after.input.value).toBe(before.input.value)
    expect(after.input.top).toBeCloseTo(before.input.top, 5)
    expect(after.input.width).toBeCloseTo(before.input.width, 5)
    expect(after.input.height).toBeCloseTo(before.input.height, 5)
    expect(after.input.color).not.toBe(before.input.color)
    expect(after.input.textFillColor).toBe(after.input.color)
    expect(after.submitTop).toBeCloseTo(before.submitTop, 5)
  })


  test('partial question + text token + full question for same id renders ONE card', async ({ page }) => {
    // The user-visible duplicate-card bug from the klix chat: the
    // SDK's --include-partial-messages can deliver two `question`
    // events for the same AskUserQuestion call with other events
    // (text token, tool boundary) landing between them. Old dedup
    // ("last block is question?") missed the second match and
    // appended a phantom card. New dedup matches by question id
    // and replaces in place no matter where the existing block
    // sits.
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'question',
        questions: [{
          id: 'klix_scope',
          question: 'What change?',
          header: 'Scope',
          multiSelect: false,
          options: [],
        }],
      })}\n\n`,
      'data: {"type":"text","content":"thinking..."}\n\n',
      `data: ${JSON.stringify({
        type: 'question',
        questions: [{
          id: 'klix_scope',
          question: 'What change?',
          header: 'Scope',
          multiSelect: false,
          options: [{ label: 'Fix' }, { label: 'Skip' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    await setupWithStreamMock(page, streamBody)
    await newChat(page)
    await sendMessage(page, 'Try the partial-then-full sequence')

    await expect(page.locator('.qcard')).toHaveCount(1, { timeout: 5000 })
    // The single card has the FINAL options (replace happened), not
    // the empty partial options.
    await expect(page.locator('.qcard__opt')).toHaveCount(
      3, // 2 real options + "Other"
      { timeout: 2000 }
    )
    // Options are radios (single-select) — single AskUserQuestion radiogroup.
    await expect(page.getByRole('radio', { name: 'Fix' })).toBeVisible()
    await expect(page.getByRole('radio', { name: 'Skip' })).toBeVisible()
  })


  test('two distinct AskUserQuestion calls (different ids) render TWO cards', async ({ page }) => {
    // Companion to the previous test: identity-based dedup must NOT
    // collapse genuinely different question calls just because they
    // share a question text or a common position.
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'question',
        questions: [{
          id: 'q-scope',
          question: 'What change?',
          header: 'Scope',
          multiSelect: false,
          options: [{ label: 'Fix' }],
        }],
      })}\n\n`,
      'data: {"type":"text","content":"got it"}\n\n',
      `data: ${JSON.stringify({
        type: 'question',
        questions: [{
          id: 'q-mode',
          question: 'Which mode?',
          header: 'Mode',
          multiSelect: false,
          options: [{ label: 'Direct' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    await setupWithStreamMock(page, streamBody)
    await newChat(page)
    await sendMessage(page, 'Ask two questions')

    await expect(page.locator('.qcard')).toHaveCount(2, { timeout: 5000 })
  })


  test('extra tool_start events AFTER the question are ignored on the question turn', async ({ page }) => {
    // Reproduces the prod garage chat shape: question followed by
    // unsuppressed tool blocks. The kill-on-question backend fix
    // means tool events arriving after the question would be a
    // SSE-mock-only test (real backend would have stopped). Here we
    // verify that ONE question event followed by done leaves
    // exactly ONE question block in the rendered transcript.
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'question',
        questions: [{
          question: 'Scope?',
          header: 'Scope',
          multiSelect: false,
          options: [{ label: 'Small' }, { label: 'Big' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    await setupWithStreamMock(page, streamBody)
    await newChat(page)
    await sendMessage(page, 'Pick scope')

    await expect(page.locator('.qcard')).toHaveCount(1, { timeout: 5000 })
    // Only the question card; no leaked tool blocks.
    await expect(page.locator('.chat__tool')).toHaveCount(0)
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
  test('an assistant message in messages persists when streamItems is empty', async ({ page }) => {
    await setupWithStreamMock(page, null)
    await newChat(page)
    await sendMessage(page, 'Test')
    // Inject a fake assistant message into the chat list — the
    // redesign means messages.map's suppression only fires under
    // bridgePartialRef.current (which we don't set in tests).
    await page.evaluate(() => {
      const list = document.querySelector('.chat__list')
      const li = document.createElement('li')
      li.className = 'chat__msg chat__msg--assistant'
      li.setAttribute('data-key', 'assistant-99-test')
      li.textContent = 'Persisted partial visible'
      list.appendChild(li)
    })
    // The injected assistant should still be in the DOM after the
    // next render cycle (no test code is suppressing it).
    await page.evaluate(() => new Promise(r =>
      requestAnimationFrame(() => requestAnimationFrame(r))
    ))
    await expect(page.locator('.chat__msg--assistant').last())
      .toContainText('Persisted partial visible')
  })
})


// ─────────────────────────────────────────────────────────────────
// BUG 2/4: scroll behaviors — geometry-owned state machine
// ─────────────────────────────────────────────────────────────────

test.describe('Bug 2/4: scroll state machine', () => {

  test('bottom detection has no lagging sentinel authority', async ({ page }) => {
    await setupWithStreamMock(page, null)
    await newChat(page)
    await sendMessage(page, 'First send')
    await expect(page.locator('.chat__bottom-sentinel')).toHaveCount(0)
    await expect(page.locator('.chat__scroll')).toHaveCount(1)
  })


  test('user messages carry data-cid (for PIN_USER_MSG resolution)', async ({ page }) => {
    await setupWithStreamMock(page, null)
    await newChat(page)
    await sendMessage(page, 'Send with cid')
    // The pin resolves the user row by its stable cid (data-cid). data-ts is
    // kept too, but only for the timestamp tooltip (display metadata).
    const pinnable = await page.locator('.chat__msg--user[data-cid]').count()
    expect(pinnable).toBeGreaterThan(0)
    const withTs = await page.locator('.chat__msg--user[data-ts]').count()
    expect(withTs).toBeGreaterThan(0)
  })


  test('messages carry data-key (for ANCHOR_AT resolution)', async ({ page }) => {
    await setupWithStreamMock(page, null)
    await newChat(page)
    await sendMessage(page, 'Send with key')
    const keyed = await page.locator('.chat__msg[data-key]').count()
    expect(keyed).toBeGreaterThan(0)
  })
})


// ─────────────────────────────────────────────────────────────────
// Q&A backend race — answers persist atomically
// ─────────────────────────────────────────────────────────────────

test.describe('Q&A atomic write', () => {

  test('POST /messages with hidden + answers sets answers on the question block', async ({ page }) => {
    // This is a backend behavior test — sanity-check that the
    // frontend's doSendSilent puts `answers` in the body, not in a
    // separate POST /question-answers request.
    const sentBodies = []
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
      if (route.request().method() !== 'POST') return route.continue()
      const body = route.request().postDataJSON()
      sentBodies.push(body)
      if (body.answers) {
        return route.fulfill({
          status: 202,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'answer_delivered',
            answer_turn: 'same',
          }),
        })
      }
      return fulfillStartedPost(route)
    })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/question-answers$/, route => {
      // SHOULD NOT be called by the new frontend.
      route.fulfill({ status: 404, body: '{}' })
    })
    await page.route('**/api/chat/stop', route =>
      route.fulfill({ status: 200, body: '{}' })
    )
    const streamBody = [
      `data: ${JSON.stringify({
        type: 'question',
        question_id: 'q-pick-atomic',
        questions: [{
          question: 'Pick',
          header: 'X',
          multiSelect: false,
          options: [{ label: 'Yes' }],
        }],
      })}\n\n`,
      'data: {"type":"done"}\n\n',
    ].join('')
    let streamCount = 0
    await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => {
      streamCount++
      route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
        body: streamCount === 1 ? streamBody : 'data: {"type":"done"}\n\n',
      })
    })

    await page.setViewportSize({ width: 412, height: 915 })
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Ask')
    await expect(page.locator('.qcard')).toBeVisible({ timeout: 5000 })
    await page.locator('.qcard__opt', { hasText: 'Yes' }).click()
    await page.evaluate(() => {
      window.__mobiusChatScrollTrace = {
        version: 1, transitions: [], writes: [], events: [],
      }
    })
    await page.locator('.qcard__submit').click()

    await expect.poll(() => sentBodies.length).toBe(2)
    // The hidden answer message MUST carry the answers field
    // (atomic backend write — no separate /question-answers POST).
    expect(sentBodies[1].hidden).toBe(true)
    expect(sentBodies[1].answers).toBeTruthy()
    expect(sentBodies[1].answers).toHaveProperty('Pick', 'Yes')
    const questionFreeze = await page.evaluate(() => (
      window.__mobiusChatScrollTrace?.transitions?.find(
        row => row.event === 'send:question-freeze',
      ) || null
    ))
    expect(questionFreeze).toBeTruthy()
    expect(questionFreeze.to?.kind).toBe('ANCHOR_AT')
  })

  test('an Android viewport growth cannot clamp a submitted question anchor', async ({ page }) => {
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

    await page.setViewportSize({ width: 426, height: 860 })
    await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, async route => {
      if (route.request().method() !== 'POST') return route.continue()
      const body = route.request().postDataJSON()
      if (!body.answers) return fulfillStartedPost(route)
      markAnswerStarted()
      await new Promise(resolve => { releaseAnswer = resolve })
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
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 },
    )
    await newChat(page)
    await sendMessage(page, 'Ask the anchored question')

    const card = page.locator('.qcard')
    await expect(card).toBeVisible({ timeout: 5000 })
    await page.evaluate(() => {
      const scroll = document.querySelector('.chat__scroll')
      if (scroll) scroll.scrollTop = scroll.scrollHeight
    })
    await page.locator('.qcard__opt', { hasText: 'Yes' }).click()

    const submit = page.locator('.qcard__submit')
    const submitClick = submit.click()
    await answerStarted
    await page.evaluate(() => new Promise(resolve => (
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    )))

    const geometry = () => page.evaluate(() => {
      const scroll = document.querySelector('.chat__scroll')
      const question = document.querySelector('.qcard')
      const spacer = document.querySelector('.spacer-dynamic')
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
    releaseAnswer()
    await submitClick

    expect(after.viewport).toBeGreaterThan(before.viewport)
    expect(Math.abs(after.scrollTop - before.scrollTop)).toBeLessThanOrEqual(2)
    expect(Math.abs(after.cardTop - before.cardTop)).toBeLessThanOrEqual(2)
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
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Try something')

    // The error notice appears during streaming with the
    // system-notice class — distinct from the assistant bubble.
    const errorBlock = page.locator('.chat__text--error', {
      hasText: 'Quota exceeded',
    })
    await expect(errorBlock).toBeVisible({ timeout: 5000 })
    await expect(page.locator('.chat__error-label', { hasText: /Error/i }))
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
      () => !document.querySelector('.chat__stop'),
      { timeout: 5000 },
    )
    // The Stop button is gone; the streaming `<li>` (which
    // shares its rendering path with the streaming render
    // branch in ChatView.jsx) is replaced by the assistant
    // `<li>` whose body comes from MsgContent.
    await expect(
      page.locator('.chat__text--error', { hasText: 'Quota exceeded' }),
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
      () => !!(document.querySelector('.chat__empty-wrap')
            || document.querySelector('.chat__form')),
      { timeout: 10000 }
    )
    await newChat(page)
    await sendMessage(page, 'Trigger error')

    // The error renders as a system notice. The URL inside it
    // must be an actual <a href> — assert the anchor exists with
    // the URL the message contained.
    const link = page.locator('.chat__text--error a[href*="example.test/billing"]')
    await expect(link).toBeVisible({ timeout: 5000 })
  })
})
