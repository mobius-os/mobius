/* The chat has one footer, on the reply at its very end: its time, Copy of
 * everything the agent said since the owner last spoke, and Branch. Replies
 * above it have no row at all, so there is no gap between them. While the
 * agent works the footer's line is reserved but empty.
 * #1290 attached copy to every assistant row and #1342 reverted that. */
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
const { default: Reply, agentStretches } = await vite.ssrLoadModule('/src/components/ChatView/AssistantReply.jsx')
const { assistantReplyGroups } = await vite.ssrLoadModule('/src/components/ChatView/assistantReplies.js')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
const { replyProse } = await vite.ssrLoadModule('/src/components/ChatView/messageCopy.js')
const proseText = messages => replyProse(messages).map(part => part.markdown).join('\n\n')

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
      useDbActivePayload: true, chatId: 'fixture', footer: 'settled', stretchSourceRows: [],
      stretchStartKey: group.rows[0].key, stretchCopyable: replyProse(messages).length > 0,
      ...props,
    })))
}
const metaRows = html => html.match(/class="chat__msg-meta[ "]/g) || []
// The reply's action row alone: an error card carries its own Copy.
const actionRow = html => html.slice(html.indexOf('class="chat__msg-meta'))

test('the footer reply ends with exactly one start-edge action row holding Copy', () => {
  const html = renderReply([turn])
  assert.equal(metaRows(html).length, 1)
  assert.match(html, /chat__msg-meta chat__msg-meta--start"/, 'hidden until tapped or hovered')
  assert.match(renderReply([turn], { metaVisible: true }), /chat__msg-meta chat__msg-meta--start chat__msg-meta--visible"/,
    'a tap reveals it, as on owner rows')
  assert.match(html, /aria-label="Copy message"/)
  assert.ok(html.indexOf('chat__msg-meta') > html.indexOf('Nothing else is pending.'),
    'the action row sits after the reply’s last text, not beside any earlier part')
})

test('a reply above the end of the chat has no action row, so no gap', () => {
  assert.equal(metaRows(renderReply([turn], { footer: null })).length, 0)
})

test('the footer retains its accessible semantics', () => {
  // CSS owns visibility; an aria-hidden ancestor would leave buttons
  // focusable but without accessible semantics.
  const row = renderReply([turn]).match(/<div class="chat__msg-meta[^>]*>/)[0]
  assert.doesNotMatch(row, /aria-hidden/)
})

test('a stretch runs across hidden carriers and ends where the owner speaks', () => {
  const reply = (id, extra = {}) => ({ role: 'assistant', id, ts: turn.ts, blocks: [{ type: 'text', content: id }], ...extra })
  const owner = { role: 'user', content: 'Next', ts: turn.ts }
  const answered = { role: 'user', content: 'Answer', hidden: true, kind: 'continuation', ts: turn.ts }
  const stretches = messages => [...agentStretches(messages, assistantReplyGroups(messages))]
    .map(([end, groups]) => [messages[end.start].id, groups.map(group => messages[group.start].id)])
  assert.deepEqual(stretches([reply('a'), answered, reply('b')]), [['b', ['a', 'b']]],
    'an answered card is not the owner speaking')
  assert.deepEqual(stretches([reply('a'), answered, reply('b'), owner, reply('c')]),
    [['b', ['a', 'b']], ['c', ['c']]])
  assert.deepEqual(stretches([owner, reply('a'), owner]), [['a', ['a']]])
  // ChatView appends the live reply as a placeholder group; it extends the
  // stretch of the replies before it.
  assert.deepEqual(stretches([reply('a'), answered, { role: 'assistant', id: 'live', blocks: [] }]),
    [['live', ['a', 'live']]])
})

test('a stretch lists its References once, after its last reply', () => {
  const sourced = { ...turn, source_ref: { message_index: 4, count: 3 } }
  assert.doesNotMatch(renderReply([sourced], { stretchSourceRows: null }), /chat__sources/,
    'a reply inside a stretch lists none')
  assert.match(renderReply([turn], { stretchSourceRows: [sourced] }), /References/,
    'the stretch’s last reply lists the sources of the replies before it')
})

test('while the agent works the footer line is reserved but empty', () => {
  // Settling must fill the line, not add one: a followed transcript would
  // grow and jump at the end of every turn (streaming-cursor.spec).
  const empty = /<div class="chat__msg-meta chat__msg-meta--start"><\/div>/
  assert.match(renderReply([turn], { footer: 'working' }), empty,
    'a new turn clears the footer, an answered card included')
  assert.match(renderReply([turn], { isStreaming: true }), empty,
    'no time or Copy while the reply is still being written')
})

test('a stretch with no prose offers no Copy but keeps its time', () => {
  const toolsOnly = { role: 'assistant', id: 'run', ts: turn.ts, blocks: [{ type: 'tool', name: 'Bash' }] }
  const row = actionRow(renderReply([toolsOnly]))
  assert.doesNotMatch(row, /aria-label="Copy message"/)
  assert.match(row, /<time class="chat__ts"/)
})

test('Copy takes everything the reply said, progress notes included', () => {
  assert.equal(proseText([turn]),
    'Checking the board first.\n\nThe board is **up to date**.\n\nNothing else is pending.')
})

test('tools and thinking never copy; prose reads across rows and skips hidden or system rows', () => {
  assert.equal(proseText([
    { role: 'assistant', blocks: [
      { type: 'thinking', content: 'private' },
      { type: 'tool', name: 'Read', output: 'file contents' },
      { type: 'text', content: 'Part one.' },
    ] },
    { role: 'user', hidden: true, content: 'carrier' },
    { role: 'assistant', kind: 'compaction', content: 'summary' },
    { role: 'assistant', content: 'Part two.' },
  ]), 'Part one.\n\nPart two.')
})

test('an open card copies; answered and sealed cards do not', () => {
  const card = { type: 'question', questions: [{
    question: 'What did the logs show?',
    options: [{ label: 'Nothing unusual' }, { label: 'Something else' }],
  }] }
  const blocks = [{ type: 'text', content: 'Checking the logs:' }, { type: 'tool', tool: 'Bash' }]
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks, card] }]),
    'Checking the logs:\n\nWhat did the logs show?\n- Nothing unusual\n- Something else')
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks, { ...card, answers: { 0: 'A' } }] }]),
    'Checking the logs:')
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks,
    { type: 'question', secure_input: { fields: [] }, questions: [{ question: 'Service API key' }] }] }]),
  'Checking the logs:')
})

test('the footer shows an older reply’s date and time, with the full date on hover', () => {
  // Recent replies read "23 minutes ago" (dateTimeFormat.test); this one is
  // weeks old, so it shows its date.
  const html = renderReply([turn])
  assert.match(html, /<time class="chat__ts" title="21st Oct 2026, \d\d:\d\d">21st Oct 2026, /)
})

test('the footer offers Copy without Try again', () => {
  const html = renderReply([turn])
  assert.match(html, /aria-label="Copy message"/)
  assert.doesNotMatch(html, /aria-label="Try again"/)
  const timeIndex = html.indexOf('<time class="chat__ts"')
  assert.ok(timeIndex >= 0 && timeIndex < html.indexOf('aria-label="Copy message"'), 'the time leads, then Copy')
})

// A reloaded transcript folds two or more adjacent tool/thinking steps into
// one activity block (`compact_messages_for_detail`); its steps are entries.
const folded = (...items) => ({
  type: 'activity', activity_id: '0:1:3', message_index: 0, start: 1, end: 1 + items.length,
  entries: items.map((item, offset) => ({ item, idx: 1 + offset })),
  tool_count: items.filter(item => item.type === 'tool').length,
})

test('a reloaded reply with folded activity keeps its prose and Copy', () => {
  const reloaded = { role: 'assistant', id: 'run', blocks: [
    { type: 'text', content: 'The build is clean.' },
    folded({ type: 'thinking' },
      { type: 'tool', tool: 'mcp__mobius_control__checkpoint_chat', status: 'done' }),
  ] }
  assert.equal(proseText([reloaded]), 'The build is clean.')
  assert.match(actionRow(renderReply([reloaded])), /aria-label="Copy message"/)
})

test('Copy locates the prose blocks MsgContent renders, after its duplicate-text repair', () => {
  // A provider once re-sent the whole answer after a question card; rendering
  // repairs that to one block, so Copy must neither duplicate nor misplace it.
  assert.deepEqual(replyProse([
    { role: 'assistant', blocks: [{ type: 'tool', tool: 'Read' }] },
    { role: 'assistant', blocks: [
      { type: 'tool', tool: 'Bash' },
      { type: 'text', content: 'Review res' },
      { type: 'question', questions: [{ question: 'Merge?' }] },
      { type: 'text', content: 'Review result' },
    ] },
  ]), [{ message: 1, block: 1, markdown: 'Review result' }, { markdown: 'Merge?' }])
})

test('Copy numbers prose blocks as they render, past deliverables and a card’s tool twin', () => {
  // MsgContent renders neither a deliverables block in place nor the tool
  // call a question or Restart card already shows, so later text blocks carry
  // a smaller `data-markdown-block` than their raw position. Copy must read
  // the element that shows its text, not a neighbour.
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
    const parts = replyProse([message]).filter(part => Number.isInteger(part.block))
    const html = renderReply([message])
    assert.ok(parts.length > 0)
    for (const part of parts) {
      const rendered = html.split(`data-markdown-block="${part.block}"`)[1]?.split('data-markdown-block=')[0]
      assert.ok(rendered?.includes(part.markdown),
        `block ${part.block} renders “${part.markdown}”`)
    }
  }
})

// Stretch-wide Copy and footer geometry run in
// tests/hidden-reply-continuity.spec.mjs against the painted ChatView.

const failedReply = (...blocks) => ({ role: 'assistant', id: 'run', ts: turn.ts, blocks })

test('provider text that repeats an error is the error, not prose to copy', () => {
  const limit = 'You\'ve hit your session limit · resets 10:30am (UTC)'
  assert.equal(proseText([failedReply(
    { type: 'text', content: 'Checking the board.' },
    { type: 'tool', name: 'Bash' },
    { type: 'text', content: limit },
    { type: 'error', message: limit, resumable: true },
  )]), 'Checking the board.')
})

test('an error echo is dropped even when its error card is on another row', () => {
  const limit = 'Usage limit reached'
  assert.equal(proseText([
    failedReply({ type: 'text', content: 'Checking.' }, { type: 'text', content: limit }),
    failedReply({ type: 'error', message: limit, resumable: true }),
  ]), 'Checking.')
})

test('a failed reply leaves recovery to its error card, never Try again', () => {
  const stopped = failedReply(
    { type: 'tool', name: 'Bash' },
    { type: 'error', message: 'The agent stopped before finishing this turn.', resumable: true },
  )
  const html = renderReply([stopped], { onResume: () => {} })
  assert.match(html, /class="chat__resume chat__recovery-action">Resume<\/button>/)
  const row = actionRow(html)
  assert.doesNotMatch(row, /aria-label="Try again"/)
  assert.doesNotMatch(row, /aria-label="Copy message"/,
    'no prose, so the row has nothing to copy (the error card copies its own message)')
  assert.match(row, /<time class="chat__ts"/)
})

test('the footer offers Branch right after Copy', () => {
  const branched = []
  const html = renderReply([turn], { onBranch: through => branched.push(through) })
  const row = actionRow(html)
  assert.match(row, /aria-label="Branch in new chat"/)
  assert.ok(row.indexOf('aria-label="Copy message"') < row.indexOf('aria-label="Branch in new chat"'),
    'Branch sits to the right of Copy')
  assert.ok(row.indexOf('<time') < row.indexOf('aria-label="Copy message"'), 'the time leads')
  assert.doesNotMatch(renderReply([turn], { footer: 'working', onBranch: () => {} }),
    /aria-label="Branch in new chat"/, 'no Branch while the agent works')
})
