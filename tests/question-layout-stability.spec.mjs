/** A parked, durable question must not rebuild its visible reply while its
 * in-process answer is pending, accepted, streamed, and settled. The socket is
 * synthetic; every non-GET API request is intercepted before it reaches a chat. */
import { test, expect, serveRecoveryBuild } from './_recoveryBrowser.mjs'
import {
  installMockAgentProvider, mockDeliveryReady, runtimeSnapshot,
  testChatAgentSettings,
} from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block', viewport: { width: 412, height: 861 } })

test('answering a parked question keeps its card and reading geometry through the same turn', async ({ page }) => {
  const chatId = crypto.randomUUID()
  const assistantId = 'layout-stability-assistant'
  const questionId = 'q-layout-stability'
  const beforeText = 'The check is ready. Please choose how to continue.'
  const afterText = 'Continuing the same answer after your choice.'
  const question = {
    type: 'question', question_id: questionId,
    questions: [{
      question: 'Continue the check?', multiSelect: false,
      options: [{ label: 'Yes' }, { label: 'No' }],
    }],
  }
  const assistant = {
    role: 'assistant', id: assistantId, ts: 1700000600100,
    content: beforeText,
    blocks: [{ type: 'text', content: beforeText }, question],
  }
  const owner = { role: 'user', id: 'layout-stability-owner', ts: 1700000600000, content: 'Check this.' }
  let running = false
  let pendingQuestionId = questionId
  let answerPost = null
  let releaseAnswer

  await serveRecoveryBuild(page)
  await installMockAgentProvider(page)
  await mockDeliveryReady(page)
  // Registered first: Playwright tries newer, specific routes before this
  // guard. No fixture mutation may fall through to the owner's live backend.
  await page.route('**/api/**', route => route.request().method() === 'GET'
    ? route.fallback()
    : route.fulfill({ status: 501, json: { detail: 'Unexpected fixture mutation' } }))
  await page.route(/\/api\/chats(?:\?.*)?$/, route => route.request().method() === 'GET'
    ? route.fulfill({ json: [{ id: chatId, title: 'Question layout fixture', has_messages: true, running: false }] })
    : route.fallback())
  await page.route(/\/api\/apps\/?(?:\?.*)?$/, route => route.fulfill({ json: [] }))
  await page.route(new RegExp(`/api/chats/${chatId}/messages$`), async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    answerPost = route.request().postDataJSON()
    if (answerPost.question_id !== questionId || !answerPost.answers) {
      return route.fulfill({ status: 400, json: { detail: 'Expected one question answer' } })
    }
    await new Promise(resolve => { releaseAnswer = resolve })
    question.answers = answerPost.answers
    pendingQuestionId = null
    running = true
    await page.evaluate(answers => window.__layoutAcceptAnswer(answers), answerPost.answers)
    return route.fulfill({
      status: 202, contentType: 'application/json',
      json: { status: 'answer_delivered', answer_turn: 'same', answers: answerPost.answers },
    })
  })
  const runtime = () => ({
    ...runtimeSnapshot({ running, pending_question_id: pendingQuestionId }),
    active_assistant_message_id: assistantId,
    active_goal_objective: null, updated_at: null,
  })
  await page.route(new RegExp(`/api/chats/${chatId}/runtime(?:\\?.*)?$`), route => route.request().method() === 'GET'
    ? route.fulfill({ json: runtime() }) : route.fallback())
  await page.route(new RegExp(`/api/chats/${chatId}(?:\\?.*)?$`), route => route.request().method() === 'GET'
    ? route.fulfill({ json: {
      ...runtime(), id: chatId, messages: [owner, assistant], total: 2, offset: 0,
      provider: 'claude', ...testChatAgentSettings(),
    } }) : route.fallback())

  await page.addInitScript(({ chatId, assistantId, beforeText, question }) => {
    const originalFetch = window.fetch.bind(window)
    const readers = new Set()
    let acceptedAnswers = null
    let streamedText = ''
    const send = event => {
      for (const write of readers) write(event)
    }
    window.__layoutAcceptAnswer = answers => { acceptedAnswers = answers }
    window.__layoutStreamText = text => {
      streamedText = text
      send({ type: 'text_final', assistant_message_id: assistantId,
        text_item_id: 'layout-continuation', content: text })
    }
    window.__layoutFinish = () => send({ type: 'done' })
    window.__layoutReaderCount = () => readers.size
    window.fetch = (input, init) => {
      const url = new URL(String(input?.url || input), location.href)
      if (url.pathname !== `/api/chats/${chatId}/stream`) return originalFetch(input, init)
      const encoder = new TextEncoder()
      const signal = init?.signal || input?.signal
      let write
      let abort
      const cleanup = () => {
        readers.delete(write)
        signal?.removeEventListener('abort', abort)
      }
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          write = event => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
          abort = () => { cleanup(); controller.error(new DOMException('Aborted', 'AbortError')) }
          readers.add(write)
          signal?.addEventListener('abort', abort, { once: true })
          write({ type: 'stream_snapshot', assistant_message_id: assistantId,
            items: [{ type: 'text', content: beforeText },
              { ...question, ...(acceptedAnswers ? { answers: acceptedAnswers } : {}) },
              ...(streamedText ? [{ type: 'text', text_item_id: 'layout-continuation', content: streamedText }] : [])] })
          write({ type: 'catch_up_done' })
        },
        cancel: cleanup,
      }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { chatId, assistantId, beforeText, question })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chatId)}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator('[data-chat-surface="painted"]')
  const card = surface.locator('.qcard')
  await expect(card).toBeVisible()
  await card.getByRole('radio', { name: 'Yes' }).click()
  const submit = card.getByRole('button', { name: 'Submit', exact: true })
  await expect(submit).toBeEnabled()

  // A DOM reference (not a selector) detects delete/reinsert even when the
  // replacement carries the same data-key.
  await card.evaluate(element => { window.__layoutCard = element; window.__layoutProse = element.closest(".chat__reply").querySelector(".md-paragraph") })
  const frame = () => page.evaluate(() => {
    const card = document.querySelector('[data-chat-surface="painted"] .qcard')
    const reply = card?.closest('.chat__reply')
    return {
      sameCard: card === window.__layoutCard,
      sameProse: Boolean(window.__layoutProse) && reply?.querySelector(".md-paragraph") === window.__layoutProse,
      top: card?.getBoundingClientRect().top,
      replyKey: card?.closest('[data-key]')?.dataset.key,
    }
  })
  const before = await frame()
  expect(before.sameCard).toBe(true)
  expect(before.sameProse).toBe(true)
  expect(before.replyKey).toBeTruthy()

  await submit.click()
  await expect.poll(() => Boolean(answerPost && releaseAnswer)).toBe(true)
  const pending = await frame()
  expect(pending).toMatchObject({ sameCard: true, sameProse: true, replyKey: before.replyKey })
  expect(Math.abs(pending.top - before.top)).toBeLessThanOrEqual(2)

  releaseAnswer()
  await expect(card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
  const accepted = await frame()
  expect(accepted).toMatchObject({ sameCard: true, sameProse: true, replyKey: before.replyKey })
  expect(Math.abs(accepted.top - before.top)).toBeLessThanOrEqual(2)

  await page.waitForFunction(() => window.__layoutReaderCount() > 0)
  await page.evaluate(text => window.__layoutStreamText(text), afterText)
  await expect(surface.locator('.chat__msg--assistant')).toContainText(afterText)
  await expect.poll(() => surface.locator('.chat__reply').getAttribute('data-current-response')).toBe('true')
  const streaming = await frame()
  expect(streaming).toMatchObject({ sameCard: true, sameProse: true, replyKey: before.replyKey })
  expect(Math.abs(streaming.top - before.top)).toBeLessThanOrEqual(2)

  assistant.blocks.push({ type: 'text', text_item_id: 'layout-continuation', content: afterText })
  assistant.content = `${beforeText}\n\n${afterText}`
  running = false
  await page.evaluate(() => window.__layoutFinish())
  await expect.poll(() => surface.locator('.chat__reply').getAttribute('data-current-response')).toBeNull()
  await expect(surface.locator('.chat__msg--assistant')).toContainText(afterText)
  const terminal = await frame()
  expect(terminal).toMatchObject({ sameCard: true, sameProse: true, replyKey: before.replyKey })
  expect(Math.abs(terminal.top - before.top)).toBeLessThanOrEqual(2)
})
