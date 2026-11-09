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

// Node has no DOM for DOMPurify, so this double records every value the
// renderer hands it and passes it through unchanged. The hostile-input test
// below therefore proves the renderer's own guards (raw HTML dropped, link
// protocol allowlist) and that each href still reaches the sanitizer; it does
// not re-test DOMPurify's own parsing.
const vite = await createSsrRenderServer({
  noExternal: ['dompurify'],
  plugins: [{
    name: 'dompurify-ssr-recorder', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-recorder' : null,
    load: id => id === '\0dompurify-recorder'
      ? 'export const sanitized = []; export default { sanitize: value => { sanitized.push(String(value)); return String(value) } }'
      : null,
  }],
})
const { sanitized } = await vite.ssrLoadModule('\0dompurify-recorder')
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

test('a hostile closing message gets the same guards as any reply prose', () => {
  sanitized.length = 0
  const hostile = {
    ...message,
    blocks: [{
      ...card,
      question_id: 'card-hostile',
      message: [
        'Before <img src=x onerror="alert(1)"> after.',
        '',
        '<script>alert(2)</script>',
        '',
        '[run](javascript:alert(3)) [data](data:text/html,<b>x</b>) [docs](https://example.com/docs)',
        '',
        '![pixel](javascript:alert(4))',
      ].join('\n'),
    }],
  }
  const html = render(hostile, { liveQuestionId: 'card-hostile' })
  assert.doesNotMatch(html, /<script|onerror|<img src="x"/i)
  assert.doesNotMatch(html, /href="(?:javascript|data):/i)
  assert.doesNotMatch(html, /src="javascript:/i)
  assert.match(html, /href="https:\/\/example\.com\/docs"/)
  assert.ok(html.indexOf('Before') < html.indexOf('Ship it today?'))
  // Every link and image target went through the sanitizer.
  for (const target of ['javascript:alert(3)', 'https://example.com/docs', 'javascript:alert(4)']) {
    assert.ok(sanitized.includes(target), target)
  }
})
