/* The real ChatView owns retry eligibility and sending; only its API is faked. */
import { expect } from '@playwright/test'
import { createTaggedChat } from './_chatTracker.mjs'
import { mockDeliveryReady, runtimeSnapshot, testChatAgentSettings } from './_chatTestPrerequisites.mjs'

export async function mountReplyFooter(page, base, {
  running = false, pendingQuestion = false,
  latestAnswer = 'Latest completed answer.\n\n' + 'Answer detail. '.repeat(100),
} = {}) {
  await page.goto(base, { waitUntil: 'domcontentloaded' })
  const chat = await createTaggedChat(page, 'reply-footer')
  const path = `/api/chats/${chat.id}`
  const assistant = (id, content, ts) => ({ role: 'assistant', id, ts,
    blocks: [{ type: 'text', content }] })
  let messages = [
    { role: 'user', content: 'First question', cid: 'footer-first', ts: 1 },
    assistant('footer-earlier', 'Earlier completed answer.', 2),
    { role: 'user', content: 'Second question', cid: 'footer-second', ts: 3 },
    assistant('footer-latest', latestAnswer, 4),
  ]
  if (pendingQuestion) messages.at(-1).blocks.push({ type: 'question', question_id: 'footer-question',
    questions: [{ question: 'Which next step?', options: [{ label: 'Continue' }] }] })
  const runtime = () => runtimeSnapshot({ running,
    pending_question_id: pendingQuestion ? 'footer-question' : null,
    // A new run without prose must suppress retry on the still-settled latest
    // reply, not merely let AssistantReply hide a streaming reply's actions.
    active_assistant_message_id: running ? 'footer-new-run' : null,
    run_id: running ? 'footer-new-run' : null, runtime_revision: 10000000 })
  const detail = () => ({ ...chat, provider: 'claude', ...testChatAgentSettings(), ...runtime(),
    messages, total: messages.length, offset: 0, active_goal_objective: null })
  await mockDeliveryReady(page)
  await page.route(new RegExp(`${path}(?:\\?.*)?$`), route => {
    if (route.request().method() !== 'GET') return route.abort()
    return route.fulfill({ json: detail() })
  })
  await page.route(`**${path}/runtime`, route => route.fulfill({ json: runtime() }))
  await page.route(`**${path}/stream`, route => route.fulfill({ status: 204, body: '' }))
  if (running) await page.addInitScript(chatId => {
    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, init) => {
      const url = String(input?.url || input)
      if (!url.includes(`/api/chats/${chatId}/stream`)) return nativeFetch(input, init)
      // route.fulfill closes SSE, so hold a real body open before any prose.
      return Promise.resolve(new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"type":"catch_up_done"}\n\n'))
      } }), { headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, chat.id)
  await page.route(`**${path}/upload-limits`, route => route.fulfill({ json: { max_bytes: 1024 * 1024 } }))
  await page.route(`**${path}/uploads`, route => route.fulfill({ json: [{
    name: 'draft-note.txt', filename: 'draft-note.txt', size: 16,
    mime_type: 'text/plain', url: `${path}/uploads/draft-note.txt`,
  }] }))
  // Retry POSTs never reach the backend. Tests opt into the exact response
  // below only when exercising the real callback, without starting a provider.
  await page.route(`**${path}/messages`, route => route.abort())
  await page.goto(`${base}/shell/?chat=${chat.id}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator('[data-chat-surface="painted"]')
  const replies = surface.locator('.chat__reply')
  await expect(replies).toHaveCount(2)
  await expect(replies.last().getByRole('button', { name: 'Copy message' })).toBeVisible()
  if (running) await expect(surface.locator('.chat__stop')).toBeVisible()
  if (pendingQuestion) await expect(surface.locator('.qcard')).toBeVisible()
  const composer = surface.getByRole('textbox', { name: 'Message Möbius…' })
  return { surface, replies, composer, path,
    accept(body) {
      const message = { role: 'user', content: body.content, cid: body.cid, ts: 5 }
      messages = [...messages, message, assistant('footer-retried', 'Retried answer.', 6)]
      return { status: 'started', message }
    },
  }
}
