/** An answered question keeps its reply's footer line while the same answer
 * continues: the card stays mounted, the line keeps its height with References
 * held in it, and Copy returns once the answer settles. (A new turn giving the
 * line up is covered by replyFooter.test.js.) The socket is synthetic; every
 * non-GET API request is intercepted before it reaches a chat. */
import { test, expect, serveRecoveryBuild } from './_recoveryBrowser.mjs'
import {
  installMockAgentProvider, mockDeliveryReady, runtimeSnapshot,
  testChatAgentSettings,
} from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const ASSISTANT_ID = 'layout-stability-assistant'
const QUESTION_ID = 'q-layout-stability'
const BEFORE_TEXT = 'The check is ready. Please choose how to continue.'
const AFTER_TEXT = 'Continuing the same answer after your choice.'

test.use({ serviceWorkers: 'block', viewport: { width: 412, height: 861 } })

/** A chat whose last reply cites two sources and parks on one question; the
 *  server resumes the parked run in the same row when it is answered. */
async function mountParkedQuestion(page) {
  const chatId = crypto.randomUUID()
  const question = {
    type: 'question', question_id: QUESTION_ID,
    questions: [{
      question: 'Continue the check?', multiSelect: false,
      options: [{ label: 'Yes' }, { label: 'No' }],
    }],
  }
  const assistant = {
    role: 'assistant', id: ASSISTANT_ID, ts: 1700000600100,
    content: BEFORE_TEXT,
    blocks: [
      { type: 'tool', tool: 'WebSearch', tool_use_id: 'layout-search', status: 'done', input: { query: 'example' },
        sources: [{ url: 'https://example.com/a', title: 'First source' }, { url: 'https://example.org/b', title: 'Second source' }] },
      { type: 'text', content: BEFORE_TEXT },
      question,
    ],
  }
  const owner = { role: 'user', id: 'layout-stability-owner', ts: 1700000600000, content: 'Check this.' }
  const state = {
    messages: [owner, assistant], running: false, pendingQuestionId: QUESTION_ID,
    answerPost: null, releaseAnswer: null,
  }

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
  await page.route(new RegExp(`/api/chats/${chatId}/message-sources`), route => route.fulfill({ json: { sources: [] } }))
  await page.route(new RegExp(`/api/chats/${chatId}/messages$`), async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    state.answerPost = route.request().postDataJSON()
    if (state.answerPost.question_id !== QUESTION_ID || !state.answerPost.answers) {
      return route.fulfill({ status: 400, json: { detail: 'Expected one question answer' } })
    }
    await new Promise(resolve => { state.releaseAnswer = resolve })
    question.answers = state.answerPost.answers
    state.pendingQuestionId = null
    state.running = true
    await page.evaluate(answers => window.__layoutAcceptAnswer(answers), state.answerPost.answers)
    return route.fulfill({ status: 202, json: {
      status: 'answer_delivered', answer_turn: 'same', answers: state.answerPost.answers,
    } })
  })
  const runtime = () => ({
    ...runtimeSnapshot({ running: state.running, pending_question_id: state.pendingQuestionId }),
    active_assistant_message_id: ASSISTANT_ID,
    active_goal_objective: null, updated_at: null,
  })
  await page.route(new RegExp(`/api/chats/${chatId}/runtime(?:\\?.*)?$`), route => route.request().method() === 'GET'
    ? route.fulfill({ json: runtime() }) : route.fallback())
  await page.route(new RegExp(`/api/chats/${chatId}(?:\\?.*)?$`), route => route.request().method() === 'GET'
    ? route.fulfill({ json: {
      ...runtime(), id: chatId, messages: state.messages, total: state.messages.length, offset: 0,
      provider: 'claude', ...testChatAgentSettings(),
    } }) : route.fallback())

  await page.addInitScript(({ chatId, assistantId, beforeText, question, search }) => {
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
          write({ type: 'stream_snapshot', assistant_message_id: assistantId, items: [
            search, { type: 'text', content: beforeText },
            { ...question, ...(acceptedAnswers ? { answers: acceptedAnswers } : {}) },
            ...(streamedText ? [{ type: 'text', text_item_id: 'layout-continuation', content: streamedText }] : []),
          ] })
          write({ type: 'catch_up_done' })
        },
        cancel: cleanup,
      }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, { chatId, assistantId: ASSISTANT_ID, beforeText: BEFORE_TEXT, question, search: assistant.blocks[0] })

  await page.goto(`${BASE}/shell/?chat=${encodeURIComponent(chatId)}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator('[data-chat-surface="painted"]')
  // The question's reply is located through its card, so a live reply added
  // below it can never satisfy or break a locator under strict mode.
  const reply = surface.locator('.chat__reply').filter({ has: page.locator('.qcard') })
  const card = reply.locator('.qcard')
  await expect(card).toBeVisible()
  await expect(reply).toHaveCount(1)
  await card.getByRole('radio', { name: 'Yes' }).click()
  const submit = card.getByRole('button', { name: 'Submit', exact: true })
  await expect(submit).toBeEnabled()
  // A DOM reference (not a selector) detects delete/reinsert even when the
  // replacement carries the same data-key. The footer is measured separately
  // because a submit-time footer blink is a layout change, not a card remount.
  await card.evaluate(element => { window.__layoutCard = element })
  const frame = () => page.evaluate(() => {
    const card = document.querySelector('[data-chat-surface="painted"] .qcard')
    const reply = card?.closest('.chat__reply')
    const footer = reply?.querySelector(':scope > .chat__msg-meta--start')
    const references = footer?.querySelector(':scope > .chat__sources')
    return {
      sameCard: card === window.__layoutCard,
      footerHeight: footer?.getBoundingClientRect().height || 0,
      referencesHeld: references ? references.classList.contains('chat__sources--held') : null,
      copy: !!footer?.querySelector('.chat__msg-copy'),
      replyKey: card?.closest('[data-key]')?.dataset.key,
    }
  })
  return { state, reply, card, submit, frame }
}

test('a same-answer continuation keeps its footer line, holding References, until Copy returns', async ({ page }) => {
  const { state, reply, card, submit, frame } = await mountParkedQuestion(page)
  const before = await frame()
  expect(before).toMatchObject({ sameCard: true, referencesHeld: false, copy: true })
  expect(before.replyKey).toBeTruthy()
  expect(before.footerHeight).toBeGreaterThan(0)
  // Same card, same line height, References held in place, no controls.
  const expectHeldLine = next => expect(next).toMatchObject({
    sameCard: true, replyKey: before.replyKey, footerHeight: before.footerHeight,
    referencesHeld: true, copy: false,
  })

  await submit.click()
  await expect.poll(() => Boolean(state.answerPost && state.releaseAnswer)).toBe(true)
  expectHeldLine(await frame())

  state.releaseAnswer()
  await expect(card.getByRole('button', { name: 'Submitted', exact: true })).toBeVisible()
  expectHeldLine(await frame())

  await page.waitForFunction(() => window.__layoutReaderCount() > 0)
  await page.evaluate(text => window.__layoutStreamText(text), AFTER_TEXT)
  await expect(reply).toContainText(AFTER_TEXT)
  await expect.poll(() => reply.getAttribute('data-current-response')).toBe('true')
  expectHeldLine(await frame())

  const assistant = state.messages[1]
  assistant.blocks.push({ type: 'text', text_item_id: 'layout-continuation', content: AFTER_TEXT })
  assistant.content = `${BEFORE_TEXT}\n\n${AFTER_TEXT}`
  state.running = false
  await page.evaluate(() => window.__layoutFinish())
  await expect.poll(() => reply.getAttribute('data-current-response')).toBeNull()
  await expect.poll(async () => (await frame()).copy).toBe(true)
  expect(await frame()).toMatchObject({
    sameCard: true, replyKey: before.replyKey, footerHeight: before.footerHeight, referencesHeld: false,
  })
})
