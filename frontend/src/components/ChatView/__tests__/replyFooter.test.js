/* A reply has one action row, on its own line at its end, holding a Copy of
 * its answer. It appears only once the reply has settled. The latest reply
 * keeps it in view; earlier replies reveal it by tap (or hover with a mouse).
 * #1290 attached copy to every assistant row and #1342 reverted that; a reply
 * now owns exactly one row instead. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { renderWithModels } from './modelRegistryRender.js'

const location = { origin: 'http://localhost', href: 'http://localhost/shell/' }
globalThis.window = { location, innerWidth: 900 }
globalThis.location = location
const vite = await createSsrRenderServer({
  noExternal: ['dompurify'],
  plugins: [{ name: 'dompurify-ssr-stub', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-stub' : null,
    load: id => id === '\0dompurify-stub'
      ? 'export default { sanitize: value => String(value) }' : null }],
})
after(() => vite.close())
const { default: Reply } = await vite.ssrLoadModule('/src/components/ChatView/AssistantReply.jsx')
const { assistantReplyGroups } = await vite.ssrLoadModule('/src/components/ChatView/assistantReplies.js')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
const { replyOutcome } = await vite.ssrLoadModule('/src/components/ChatView/messageCopy.js')
const replyAnswerText = messages => replyOutcome(messages).answer
const summary = messages => {
  const { answer, endingError } = replyOutcome(messages)
  return { answer, failed: !!endingError }
}

const turn = {
  role: 'assistant',
  id: 'run',
  ts: Date.UTC(2026, 9, 21, 9, 52),
  blocks: [
    { type: 'text', content: 'Checking the board first.' },
    { type: 'tool', name: 'Bash', input: { command: 'ls' }, output: 'x' },
    { type: 'text', content: 'The board is **up to date**.' },
    { type: 'text', content: 'Nothing else is pending.' },
  ],
}

function renderReply(messages, props = {}) {
  const group = assistantReplyGroups(messages).get(0)
  return renderWithModels(createElement(PeerTimelineContext.Provider,
    { value: { tools: new Map(), positions: new Map() } },
    createElement(Reply, {
      replyGroup: group, activeRowIndex: -1, activeMirrorMsg: group.rows.at(-1).message,
      useDbActivePayload: true, chatId: 'fixture', onRevealMeta: () => {}, ...props,
    })))
}
const metaRows = html => html.match(/class="chat__msg-meta[ "]/g) || []
// The reply's action row alone: an error card carries its own Copy.
const actionRow = html => html.slice(html.indexOf('class="chat__msg-meta'))

test('a settled reply ends with exactly one start-edge action row holding Copy', () => {
  const html = renderReply([turn])
  assert.equal(metaRows(html).length, 1)
  assert.match(html, /chat__msg-meta chat__msg-meta--start"/)
  assert.match(html, /aria-label="Copy message"/)
  assert.ok(html.indexOf('chat__msg-meta') > html.indexOf('Nothing else is pending.'),
    'the action row sits after the reply’s last text, not beside any earlier part')
})

test('an earlier reply hides its action row until revealed', () => {
  assert.doesNotMatch(renderReply([turn]), /chat__msg-meta--visible/)
  assert.match(renderReply([turn], { metaVisible: true }), /chat__msg-meta--visible/)
})

test('a hidden action row is not also hidden from assistive technology', () => {
  // Hover reveals the row through CSS alone; an aria-hidden ancestor would
  // leave its now-focusable buttons without accessible semantics.
  const row = renderReply([turn]).match(/<div class="chat__msg-meta[^>]*>/)[0]
  assert.doesNotMatch(row, /aria-hidden/)
})

test('a streaming reply already reserves its action row, empty until it settles', () => {
  // Settling must fill the line, not add one: a followed transcript would
  // grow and jump at the end of every turn (streaming-cursor.spec).
  const html = renderReply([turn], { isStreaming: true, metaVisible: true, onTryAgain: () => {} })
  assert.equal(metaRows(html).length, 1)
  assert.match(html, /<div class="chat__msg-meta chat__msg-meta--start chat__msg-meta--visible"><\/div>/,
    'no time, Copy or Try again while the reply is still being written')
})

test('a reply with no prose offers no Copy but keeps its time', () => {
  const toolsOnly = { role: 'assistant', id: 'run', ts: turn.ts, blocks: [{ type: 'tool', name: 'Bash' }] }
  const row = actionRow(renderReply([toolsOnly]))
  assert.doesNotMatch(row, /aria-label="Copy message"/)
  assert.match(row, /<time class="chat__ts">/)
})

test('reply Copy takes the answer after the last activity, not the progress notes', () => {
  assert.equal(replyAnswerText([turn]),
    'The board is **up to date**.\n\nNothing else is pending.')
})

test('cards neither copy nor split the answer; thinking does split it', () => {
  assert.equal(replyAnswerText([{ role: 'assistant', blocks: [
    { type: 'thinking', content: 'private' },
    { type: 'text', content: 'Which option?' },
    { type: 'question', questions: [] },
  ] }]), 'Which option?')
  assert.equal(replyAnswerText([{ role: 'assistant', blocks: [
    { type: 'text', content: 'Before.' },
    { type: 'thinking', content: 'private' },
    { type: 'text', content: 'After.' },
  ] }]), 'After.')
})

test('activity after the answer does not erase it', () => {
  assert.equal(replyAnswerText([{ role: 'assistant', blocks: [
    { type: 'tool', name: 'Read' },
    { type: 'text', content: 'Pick a direction below.' },
    { type: 'tool', name: 'request_question' },
    { type: 'question', questions: [] },
  ] }]), 'Pick a direction below.')
})

test('the answer reads across a reply’s rows and skips hidden or system rows', () => {
  assert.equal(replyAnswerText([
    { role: 'assistant', blocks: [{ type: 'tool', name: 'Read' }, { type: 'text', content: 'Part one.' }] },
    { role: 'user', hidden: true, content: 'carrier' },
    { role: 'assistant', kind: 'compaction', content: 'summary' },
    { role: 'assistant', content: 'Part two.' },
  ]), 'Part one.\n\nPart two.')
})

test('the reply row shows the reply’s date and time after its actions', () => {
  const html = renderReply([turn])
  assert.match(html, /<time class="chat__ts">21st Oct 2026, /)
})

test('Try again appears only where the conversation offers it', () => {
  assert.doesNotMatch(renderReply([turn]), /aria-label="Try again"/,
    'an earlier reply, or one while a turn runs, has no Try again')
  const latest = renderReply([turn], { metaVisible: true, onTryAgain: () => {} })
  assert.match(latest, /aria-label="Try again"/)
  assert.ok(latest.indexOf('aria-label="Copy message"') < latest.indexOf('aria-label="Try again"'))
})

test('a progress note followed by real work is not the answer; the card it ends on is', () => {
  // A common turn shape: a short note, more tool work, a save, then a
  // question card with no closing prose. Copy used to take the note.
  const question = { type: 'question', questions: [{
    question: 'What did the logs show?',
    options: [{ label: 'Nothing unusual' }, { label: 'Something else' }],
  }] }
  const narration = [
    { type: 'text', content: 'The build looks **fine**. Checking the logs next:' },
    { type: 'tool', tool: 'Bash' },
    { type: 'thinking', content: 'private' },
    { type: 'tool', tool: 'mcp__mobius_control__checkpoint_chat' },
    { type: 'tool', tool: 'mcp__mobius_control__request_question' },
  ]
  assert.deepEqual(summary([{ role: 'assistant', blocks: [...narration, question] }]), {
    answer: 'What did the logs show?\n- Nothing unusual\n- Something else', failed: false,
  })
  assert.deepEqual(summary([{ role: 'assistant', blocks: narration }]),
    { answer: '', failed: false }, 'a note with no card after it copies nothing')
  assert.doesNotMatch(actionRow(renderReply([{ role: 'assistant', id: 'run', blocks: narration }])),
    /aria-label="Copy message"/)
})

test('thinking and closing bookkeeping after the answer keep it', () => {
  assert.equal(replyAnswerText([{ role: 'assistant', blocks: [
    { type: 'tool', tool: 'Read' },
    { type: 'text', content: 'The build is clean.' },
    { type: 'tool', tool: 'mobius_control:memory_remember' },
    { type: 'thinking', content: 'private' },
    { type: 'tool', tool: 'mcp__mobius_control__notify_owner' },
    { type: 'tool', tool: 'mobius_control:checkpoint_chat' },
    { type: 'tool', tool: 'mcp__mobius_control__request_question' },
    { type: 'question', questions: [{ question: 'What next?' }] },
  ] }]), 'The build is clean.')
})

// A reloaded transcript folds two or more adjacent tool/thinking steps into
// one activity block (`compact_messages_for_detail`); its steps are entries.
const folded = (...items) => ({
  type: 'activity', activity_id: '0:1:3', message_index: 0, start: 1, end: 1 + items.length,
  entries: items.map((item, offset) => ({ item, idx: 1 + offset })),
  tool_count: items.filter(item => item.type === 'tool').length,
})

test('folded bookkeeping after the answer keeps it after a reload', () => {
  const question = { type: 'question', questions: [{ question: 'Which way?', options: [{ label: 'A' }] }] }
  const reloaded = { role: 'assistant', id: 'run', blocks: [
    { type: 'text', content: 'The build is clean.' },
    folded({ type: 'thinking' },
      { type: 'tool', tool: 'mcp__mobius_control__checkpoint_chat', status: 'done' }),
  ] }
  assert.equal(replyAnswerText([reloaded]), 'The build is clean.')
  assert.match(actionRow(renderReply([reloaded])), /aria-label="Copy message"/,
    'the reloaded reply keeps its Copy')
  assert.equal(replyAnswerText([{ ...reloaded, blocks: [...reloaded.blocks, question] }]),
    'The build is clean.', 'a card after folded bookkeeping does not replace the answer')
})

test('folded real work after a note still makes it a progress note', () => {
  const question = { type: 'question', questions: [{ question: 'Which way?', options: [{ label: 'A' }] }] }
  const narration = [
    { type: 'text', content: 'Checking the logs next:' },
    folded({ type: 'tool', tool: 'Bash', status: 'done' },
      { type: 'tool', tool: 'mcp__mobius_control__checkpoint_chat', status: 'done' }),
  ]
  assert.equal(replyAnswerText([{ role: 'assistant', blocks: narration }]), '')
  assert.equal(replyAnswerText([{ role: 'assistant', blocks: [...narration, question] }]),
    'Which way?\n- A')
})

test('a sealed secret card is never copied as the answer', () => {
  assert.equal(replyAnswerText([{ role: 'assistant', blocks: [
    { type: 'text', content: 'Opening the sealed card:' },
    { type: 'tool', tool: 'Bash' },
    { type: 'tool', tool: 'mcp__mobius_control__request_secret' },
    { type: 'question', secure_input: { fields: [] }, questions: [{ question: 'Service API key' }] },
  ] }]), '')
})

test('Copy locates the answer blocks MsgContent renders, after its duplicate-text repair', () => {
  // A provider once re-sent the whole answer after a question card; rendering
  // repairs that to one block, so Copy must neither duplicate nor misplace it.
  const { answer, parts } = replyOutcome([
    { role: 'assistant', blocks: [{ type: 'tool', tool: 'Read' }] },
    { role: 'assistant', blocks: [
      { type: 'tool', tool: 'Bash' },
      { type: 'text', content: 'Review res' },
      { type: 'question', questions: [{ question: 'Merge?' }] },
      { type: 'text', content: 'Review result' },
    ] },
  ])
  assert.equal(answer, 'Review result')
  assert.deepEqual(parts, [{ message: 1, block: 1, markdown: 'Review result' }])
})

test('Copy numbers answer blocks as they render, past deliverables and a card’s tool twin', () => {
  // MsgContent renders neither a deliverables block in place nor the tool
  // call a question or Restart card already shows, so later text blocks carry
  // a smaller `data-markdown-block` than their raw position. Copy must read
  // the element that shows its answer, not a neighbour.
  const replies = [
    [
      { type: 'tool', tool: 'Bash', input: { command: 'ls' }, output: 'x' },
      { type: 'generated_files', files: [{ name: 'report.csv', size: 9, mime_type: 'text/csv' }] },
      { type: 'text', content: 'The report is attached.' },
    ],
    [
      { type: 'tool', tool: 'AskUserQuestion', input: {} },
      { type: 'question', question_id: 'merge', questions: [{ question: 'Merge?', options: [{ label: 'Yes' }] }] },
      { type: 'text', content: 'Merged as asked.' },
      { type: 'text', content: 'Nothing else changed.' },
    ],
  ]
  for (const blocks of replies) {
    const message = { role: 'assistant', id: 'run', ts: turn.ts, blocks }
    const { parts } = replyOutcome([message])
    const html = renderReply([message])
    assert.ok(parts.length > 0)
    for (const part of parts) {
      const rendered = html.split(`data-markdown-block="${part.block}"`)[1]?.split('data-markdown-block=')[0]
      assert.ok(rendered?.includes(part.markdown),
        `block ${part.block} renders “${part.markdown}”`)
    }
  }
})

// Caller eligibility, the real retry click/draft path, and footer flow geometry
// run in tests/hidden-reply-continuity.spec.mjs against the painted ChatView.

const failedReply = (...blocks) => ({ role: 'assistant', id: 'run', ts: turn.ts, blocks })

test('a reply ending on a failure card leaves recovery to that card', () => {
  const limit = 'You\'ve hit your session limit · resets 10:30am (UTC)'
  const parked = failedReply(
    { type: 'text', content: 'Checking the board.' },
    { type: 'tool', name: 'Bash' },
    { type: 'text', content: limit },
    { type: 'error', message: limit, resumable: true },
  )
  assert.deepEqual(summary([parked]), { answer: '', failed: true },
    'the provider’s echo of the limit is the error, not an answer')
  const row = actionRow(renderReply([parked], { onTryAgain: () => {}, onResume: () => {} }))
  assert.doesNotMatch(row, /aria-label="(Copy message|Try again)"/)
})

test('a stopped reply copies only the answer it was still writing, never Try again', () => {
  const narration = failedReply(
    { type: 'text', content: 'Let me check the API access.' },
    { type: 'tool', name: 'Bash' },
    { type: 'error', message: 'The agent stopped before finishing this turn.', resumable: true },
  )
  assert.deepEqual(summary([narration]), { answer: '', failed: true })
  const partial = failedReply(
    { type: 'tool', name: 'Bash' },
    { type: 'text', content: 'The board is up to' },
    { type: 'error', message: 'The agent stopped before finishing this turn.', resumable: true },
  )
  assert.deepEqual(summary([partial]), { answer: 'The board is up to', failed: true })
  const html = renderReply([partial], { metaVisible: true, onTryAgain: () => {}, onResume: () => {} })
  assert.match(html, /aria-label="Copy message"/)
  assert.doesNotMatch(html, /aria-label="Try again"/)
})

test('an error card that cannot recover leaves Try again to the latest reply', () => {
  // Only a resumable tail card owns recovery (ownsRecoveryAction); a plain
  // provider error shows neither Try now nor Resume.
  const latest = { metaVisible: true, onTryAgain: () => {}, onResume: () => {} }
  const providerError = failedReply(
    { type: 'tool', name: 'Bash' },
    { type: 'error', message: 'The provider returned an error.' },
  )
  const bare = actionRow(renderReply([providerError], latest))
  assert.match(bare, /aria-label="Try again"/)
  assert.doesNotMatch(bare, /aria-label="Copy message"/,
    'no answer, so the row has nothing to copy (the error card copies its own message)')
  assert.match(bare, /<time class="chat__ts">/)
  const partial = failedReply(
    { type: 'text', content: 'The board is up to' },
    { type: 'error', message: 'The provider returned an error.' },
  )
  const html = actionRow(renderReply([partial], latest))
  assert.match(html, /aria-label="Copy message"/)
  assert.match(html, /aria-label="Try again"/)
})

test('a resumable error card owns recovery only where it can act', () => {
  const stopped = failedReply(
    { type: 'tool', name: 'Bash' },
    { type: 'error', message: 'The agent stopped before finishing this turn.', resumable: true },
  )
  assert.doesNotMatch(actionRow(renderReply([stopped], { onTryAgain: () => {}, onResume: () => {} })),
    /aria-label="Try again"/, 'its Resume is the way on')
  assert.match(renderReply([stopped], { onTryAgain: () => {} }), /aria-label="Try again"/,
    'without a Resume path the card offers nothing, so Try again does')
})

test('an error card above a later row does not end the reply', () => {
  const later = { role: 'assistant', id: 'later', ts: turn.ts, kind: 'compaction', blocks: [] }
  const { endingError } = replyOutcome([failedReply(
    { type: 'error', message: 'Interrupted', resumable: true },
  ), later])
  assert.equal(endingError, null)
})

test('an error the reply recovered from does not count as failing', () => {
  assert.deepEqual(summary([failedReply(
    { type: 'error', message: 'Temporary glitch' },
    { type: 'tool', name: 'Bash' },
    { type: 'text', content: 'Done.' },
  )]), { answer: 'Done.', failed: false })
})
