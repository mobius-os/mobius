/* A saved owner card can carry the turn's closing message. It renders as the
 * reply's ordinary prose directly above its card, live and after reload, and
 * Copy reads it as the answer. Cards saved without one render unchanged. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

globalThis.location = { origin: 'http://localhost' }
globalThis.window = { location: globalThis.location, innerHeight: 800 }

const vite = await createSsrRenderServer({
  noExternal: ['dompurify'],
  plugins: [{
    name: 'dompurify-ssr-stub', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-stub' : null,
    load: id => id === '\0dompurify-stub' ? 'export default { sanitize: value => String(value) }' : null,
  }],
})
after(() => vite.close())
const { default: MsgContent } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const { renderedBlockEntries, upsertQuestionItem } = await vite.ssrLoadModule(
  '/src/components/ChatView/streamReducers.js',
)
const { streamItemToBlock } = await vite.ssrLoadModule('/src/components/ChatView/streamPromotion.js')
const { replyOutcome } = await vite.ssrLoadModule('/src/components/ChatView/messageCopy.js')
const { questionKey } = await vite.ssrLoadModule('/src/components/ChatView/questionKey.js')

const card = {
  type: 'question',
  question_id: 'card-1',
  response_mode: 'continuation',
  message: 'I fixed the build. **One** choice remains.',
  questions: [{ id: 'q', header: 'Your choice', question: 'Ship it today?', options: [] }],
}
const message = {
  role: 'assistant',
  id: 'reply',
  blocks: [
    { type: 'text', content: 'Checking the build.' },
    { type: 'tool', tool: 'Bash', tool_use_id: 'b1', input: 'npm test', output: 'ok', status: 'done' },
    card,
  ],
}

function render(msg, extra = {}) {
  return renderToStaticMarkup(createElement(MsgContent, {
    msg, chatId: 'chat-1', messageKey: 'reply', isLastMsg: true, liveQuestionId: 'card-1', ...extra,
  }))
}

test('the closing message renders as prose directly above its card', () => {
  const entries = renderedBlockEntries(message.blocks)
  assert.deepEqual(entries.map(({ item }) => item.type), ['text', 'tool', 'text', 'question'])
  assert.equal(entries[2].item.content, card.message)
  // One stable block per card keeps memoized Markdown from re-rendering.
  assert.equal(renderedBlockEntries(message.blocks)[2].item, entries[2].item)

  const html = render(message)
  const prose = html.indexOf('<strong>One</strong> choice remains.')
  assert.ok(prose > 0, html)
  assert.ok(prose < html.indexOf('Ship it today?'))
  assert.match(html, /data-markdown-block="2"/)
})

test('a card saved without a closing message renders unchanged', () => {
  const { message: _, ...older } = card
  const blocks = [...message.blocks.slice(0, 2), older]
  assert.deepEqual(renderedBlockEntries(blocks).map(({ item }) => item.type), ['text', 'tool', 'question'])
  assert.equal(render({ ...message, blocks }).includes('choice remains'), false)
})

test('the live surface keeps the message and owns it while it shows the card', () => {
  const live = upsertQuestionItem([], { ...card })
  const replayed = upsertQuestionItem(live, { type: 'question', question_id: 'card-1', questions: card.questions })
  assert.equal(replayed[0].message, card.message)
  assert.equal(streamItemToBlock(replayed[0]).message, card.message)

  const suppressed = render(message, { suppressedQuestionKeys: new Set([questionKey(card)]) })
  assert.equal(suppressed.includes('choice remains'), false)
  assert.equal(suppressed.includes('Ship it today?'), false)
})

test('Copy reads the closing message as the reply answer', () => {
  assert.equal(replyOutcome([message]).answer, card.message)
})
