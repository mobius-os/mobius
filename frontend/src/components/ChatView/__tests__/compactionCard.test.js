import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { default: CompactionCard } = await vite.ssrLoadModule('/src/components/ChatView/CompactionCard.jsx')
after(() => vite.close())

const render = msg => renderToStaticMarkup(createElement(CompactionCard, { msg }))
const directRow = extra => ({
  kind: 'compaction',
  handover: 'direct',
  content: 'Digest: the saved summary.\n\nRecent: the last messages.',
  ...extra,
})

test('a direct handover row says which provider the chat switched to', () => {
  const html = render(directRow({ to_provider: 'codex' }))
  assert.match(html, />Switched to OpenAI Codex</)
  assert.match(html, /continuing from this chat’s digest and recent messages/)
  assert.doesNotMatch(html, /Conversation summarized/)
})

test('a direct handover row with an unknown provider still reads as a switch', () => {
  const html = render(directRow())
  assert.match(html, />Provider switched</)
  assert.match(html, /continuing from this chat’s digest and recent messages/)
})

test('a direct handover row with saved text is expandable and one without is a plain divider', () => {
  assert.match(render(directRow({ to_provider: 'claude' })), /<button[^>]*aria-expanded="false"/)
  assert.doesNotMatch(render(directRow({ content: '' })), /<button/)
})

test('a row without a handover marker keeps the summary wording', () => {
  const plain = render({ kind: 'compaction', content: 'Goal: keep context.' })
  assert.match(plain, />Conversation summarized</)
  assert.doesNotMatch(plain, /continuing from/)

  const older = render({ kind: 'compaction', content: 'Goal: keep context.', to_provider: 'codex' })
  assert.match(older, />Conversation summarized</)
  assert.match(older, /before switching to OpenAI Codex/)
})
