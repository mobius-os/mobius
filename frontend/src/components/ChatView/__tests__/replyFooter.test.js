/* One footer per completed answer stretch; internal runs add none. */
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
const { agentStretches, footerCopyParts, footerHasCopy, replyFooters, stretchProse } = await vite.ssrLoadModule('/src/components/ChatView/replyFooter.js')
const { assistantReplyGroups } = await vite.ssrLoadModule('/src/components/ChatView/assistantReplies.js')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
const { default: MetaRow } = await vite.ssrLoadModule('/src/components/ChatView/MessageMetaRow.jsx')
const { replyProse } = await vite.ssrLoadModule('/src/components/ChatView/messageCopy.js')
const proseText = (messages, options) => replyProse(messages, options).map(part => part.markdown).join('\n\n')

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
      // ChatView offers Copy exactly when the stretch's prose has a part.
      copy: stretchProse([group]).length ? () => null : undefined,
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

test('an internal reply before the stretch end has no action row, so no gap', () => {
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
  assert.match(renderReply([sourced]), /References/, 'its own sources too')
  assert.doesNotMatch(renderReply([sourced], { isStreaming: true }), /chat__sources/,
    'only once the reply has settled')
})

test('References sit after time, Copy and Branch inside the same autohiding footer', () => {
  const sourced = { ...turn, source_ref: { message_index: 4, count: 3 } }
  const row = actionRow(renderReply([sourced], { onBranch: () => {} }))
  assert.ok(row.indexOf('chat__sources-label') > row.indexOf('aria-label="Branch in new chat"'))
  assert.match(row, /chat__sources-body[\s\S]*<\/section><\/div>/,
    'the disclosure body belongs to the same footer, so expanding holds all its controls')
})

test('without sources the footer does not add a disclosure or change its row', () => {
  const row = actionRow(renderReply([turn], { onBranch: () => {} }))
  assert.doesNotMatch(row, /chat__sources/)
  assert.match(row, /aria-label="Copy message"/)
  assert.match(row, /aria-label="Branch in new chat"/)
})

test('an internal reply cannot create another action footer or References', () => {
  const sourced = { ...turn, source_ref: { message_index: 4, count: 3 } }
  const html = renderReply([sourced], { footer: null, stretchSourceRows: null })
  assert.equal(metaRows(html).length, 0)
  assert.doesNotMatch(html, /References/)
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

test('an open card copies; answered, completed, stopped and sealed cards do not', () => {
  const card = { type: 'question', question_id: 'logs', questions: [{
    question: 'What did the logs show?',
    options: [{ label: 'Nothing unusual' }, { label: 'Something else' }],
  }] }
  const blocks = [{ type: 'text', content: 'Checking the logs:' }, { type: 'tool', tool: 'Bash' }]
  const open = { liveQuestionId: 'logs' }
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks, card] }], open),
    'Checking the logs:\n\nWhat did the logs show?\n- Nothing unusual\n- Something else')
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks, { ...card, answers: { 0: 'A' } }] }], open),
    'Checking the logs:')
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks, card] }]), 'Checking the logs:',
    'no longer the chat’s open question: stopped or superseded')
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks,
    { ...card, platform_action: { type: 'restart', version: 2, status: 'activated' } }] }], open),
  'Checking the logs:', 'a restart card already acted on')
  assert.equal(proseText([{ role: 'assistant', blocks: [...blocks,
    { type: 'question', question_id: 'logs', secure_input: { fields: [] }, questions: [{ question: 'Service API key' }] }] }], open),
  'Checking the logs:')
})

test('the footer shows an older reply’s date and time, with the full date on hover', () => {
  // Recent replies read "23 minutes ago" (dateTimeFormat.test); this one is
  // weeks old, so it shows its date.
  const html = renderReply([turn])
  assert.match(renderReply([{ ...turn, run_ended_ts: turn.ts }]), /<time class="chat__ts" title="21st Oct 2026, \d\d:\d\d">21st Oct 2026, /)
  assert.match(html, /title="Started 21st Oct 2026, /, 'a run not yet known to have ended reads as its start')
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
      { type: 'question', question_id: 'merge', questions: [{ question: 'Merge?' }] },
      { type: 'text', content: 'Review result' },
    ] },
  ], { liveQuestionId: 'merge' }), [{ message: 1, block: 1, markdown: 'Review result' },
    { markdown: 'Merge?', card: { questions: [{ question: 'Merge?', options: [] }] } }])
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

// Which reply holds the footer, in what state, and the stretch it copies.
const row = (id, extra = {}) => ({ role: 'assistant', id, ts: turn.ts, blocks: [{ type: 'text', content: id }], ...extra })
const ownerRow = { role: 'user', content: 'Next', ts: turn.ts }
const cardAnswer = { role: 'user', content: 'Answer', hidden: true, kind: 'continuation', ts: turn.ts }
function footerOf(messages, options = {}) {
  const replyGroups = assistantReplyGroups(messages)
  const lastVisibleIndex = messages.findLastIndex(message => !message.hidden)
  return replyFooters({ messages, replyGroups, lastVisibleIndex, ...options }).current
}
const firstIds = (messages, footer) => footer.groups.map(group => messages[group.start].id)

test('the footer sits on the chat’s last reply and spans its stretch', () => {
  const messages = [ownerRow, row('a'), cardAnswer, row('b')]
  const footer = footerOf(messages)
  assert.equal(footer.group.start, 3)
  assert.equal(footer.state, 'settled')
  assert.deepEqual(firstIds(messages, footer), ['a', 'b'], 'an answered card does not end the stretch')
  assert.equal(footer.start, 1, 'the stretch starts after the owner’s row')
  assert.equal(footer.truncated, false)
  assert.equal(footerOf([row('a'), ownerRow]), null, 'no footer once the owner has spoken')
})

test('earlier completed answers retain one footer each while a newer answer runs', () => {
  const messages = [ownerRow, row('a'), cardAnswer, row('b'), ownerRow, row('c'), ownerRow, row('live')]
  const replyGroups = assistantReplyGroups(messages)
  const options = { messages, replyGroups,
    lastVisibleIndex: 7, liveIndex: 7, turnStarting: true, settledEnd: 5 }
  const footers = [...replyFooters(options).byGroup.values()]
  assert.deepEqual(footers.map(footer => [footer.group.start, footer.state,
    firstIds(messages, footer)]), [
    [3, 'settled', ['a', 'b']], [5, 'settled', ['c']], [7, 'working', ['live']],
  ])
  assert.deepEqual(stretchProse(footers[0].groups).map(part => part.markdown), ['a', 'b'])
  assert.deepEqual(stretchProse(footers[1].groups).map(part => part.markdown), ['c'])
  assert.deepEqual(footers[0].sourceRows.map(message => message.id), ['a'],
    'an answer lists the References of its earlier replies with its last one')
  // A recompute over the same groups (a streamed frame) keeps that array.
  assert.equal([...replyFooters(options).byGroup.values()][0].sourceRows, footers[0].sourceRows)
  assert.deepEqual([...replyFooters({ ...options, liveIndex: -1, lastVisibleIndex: 6 }).byGroup.values()]
    .map(footer => footer.state), ['settled', 'settled', 'settled'],
    'a submitted owner row cannot remove earlier complete footers')
})

test('a stretch that reaches the loaded page’s first row may have begun earlier', () => {
  const messages = [cardAnswer, row('a'), cardAnswer, row('b')]
  assert.deepEqual({ ...footerOf(messages, { offset: 40 }), group: undefined, groups: undefined, sourceRows: undefined },
    { group: undefined, groups: undefined, sourceRows: undefined, state: 'settled', end: 43, start: 0, truncated: true })
  assert.equal(footerOf(messages).truncated, false, 'the whole chat is loaded')
})

test('a starting turn clears the footer; only a reply the turn writes reserves its line', () => {
  const messages = [ownerRow, row('a')]
  const settledEnd = footerOf(messages).end
  assert.equal(footerOf(messages, { turnStarting: true, settledEnd }).state, null,
    'sending or answering a card clears the footer at once')
  assert.equal(footerOf(messages, { turnStarting: true, settledEnd, liveIndex: 1 }).state, null,
    'the live surface briefly reselecting the settled reply does not bring its line back')
  const live = [...messages, ownerRow, { role: 'assistant', id: 'live', blocks: [] }]
  const working = footerOf(live, { turnStarting: true, settledEnd, liveIndex: 3 })
  assert.equal(working.group.start, 3)
  assert.equal(working.state, 'working')
})

test('a turn continuing the reply that held the footer keeps its line reserved', () => {
  // A hidden steer joins the same run's next row into the same reply group,
  // so its first row (and the group's key) is unchanged; its end moves.
  const steer = { role: 'user', hidden: true, steered: true, source_work_id: 'run', kind: 'peer_message' }
  const settled = [ownerRow, row('run')]
  const settledEnd = footerOf(settled).end
  const resumed = [...settled, steer, row('run:assistant:1')]
  const footer = footerOf(resumed, { turnStarting: true, settledEnd, liveIndex: 3 })
  assert.equal(footer.group.start, 1, 'the same reply')
  assert.equal(footer.state, 'working', 'its line stays, so settling again never moves the transcript')
})

test('the answer before a visible steer stays working until its run settles', () => {
  // A visible steer ends the answer before it, but the run that wrote it
  // continues below the steer: no time, Copy or Branch on an unfinished run.
  const steer = { role: 'user', id: 'steer', content: 'Also do this', steered: true, kind: 'steer', ts: turn.ts }
  const messages = [ownerRow, row('run'), steer, row('run:assistant:1')]
  const replyGroups = assistantReplyGroups(messages, { activeIndex: 3 })
  const options = { messages, replyGroups, liveIndex: 3, lastVisibleIndex: 3, turnStarting: true, settledEnd: 1 }
  const states = footers => [...footers.byGroup.values()].map(footer => [messages[footer.group.start].id, footer.state])
  assert.deepEqual(states(replyFooters({ ...options, runningRoot: 'run' })),
    [['run', 'working'], ['run:assistant:1', 'working']])
  assert.deepEqual(states(replyFooters({ ...options, liveIndex: -1, turnStarting: false })),
    [['run', 'settled'], ['run:assistant:1', 'settled']], 'both settle with the run')
  assert.deepEqual(states(replyFooters({ ...options, runningRoot: 'other-run' })),
    [['run', 'settled'], ['run:assistant:1', 'working']], 'another run leaves a complete answer complete')
  const working = [...replyFooters({ ...options, runningRoot: 'run' }).byGroup.values()][0]
  assert.equal(footerCopyParts(working, { messages }), null, 'nothing to copy until it settles')
})

test('whether Copy shows agrees with the displayed text for answers joined across a steer or a continuation', () => {
  const steer = { role: 'user', hidden: true, steered: true, source_work_id: 'run', kind: 'peer_message' }
  const text = content => ({ type: 'text', content })
  const tool = { type: 'tool', name: 'Bash', input: { command: 'ls' }, output: 'x' }
  const reply = (id, ...blocks) => ({ role: 'assistant', id, ts: turn.ts, blocks })
  const limit = 'Usage limit reached'
  const answers = {
    'a steer replaying the sealed prose': [ownerRow, reply('run', text('The frame')), steer,
      reply('run:assistant:1', text('The framework survives.'))],
    'a steer whose continuation only works': [ownerRow, reply('run', text('Checking.')), steer, reply('run:assistant:1', tool)],
    'a steer between tool-only rows': [ownerRow, reply('run', tool), steer, reply('run:assistant:1', tool)],
    'a continuation after an answered card': [ownerRow, reply('a', text('First part.')), cardAnswer, reply('b', tool)],
    'a continuation that only repeats its error': [ownerRow, reply('a', tool), cardAnswer,
      reply('b', text(limit), { type: 'error', message: limit })],
    'a continuation adding prose after tools': [ownerRow, reply('a', tool), cardAnswer, reply('b', text('Done.'))],
  }
  for (const [shape, messages] of Object.entries(answers)) {
    const replyGroups = assistantReplyGroups(messages)
    const { current } = replyFooters({ messages, replyGroups, lastVisibleIndex: messages.length - 1 })
    const parts = footerCopyParts(current, { messages }) || []
    assert.equal(footerHasCopy(current), parts.length > 0, shape)
  }
})

test('Copy shows for a settled answer with prose, and only once an earlier start is read', () => {
  const footersOf = (messages, options = {}) => [...replyFooters({
    messages, replyGroups: assistantReplyGroups(messages, options), lastVisibleIndex: messages.length - 1, ...options,
  }).byGroup.values()]
  const [prose] = footersOf([ownerRow, row('a')])
  assert.equal(footerHasCopy(prose), true)
  assert.equal(footerHasCopy({ ...prose, state: 'working' }), false, 'nothing until it settles')
  const [tools] = footersOf([ownerRow, row('t', { blocks: [{ type: 'tool', name: 'Bash' }] })])
  assert.equal(footerHasCopy(tools), false, 'tools only: nothing to copy')
  // An answer reaching the page's first row may have begun before it.
  const [partial] = footersOf([cardAnswer, row('t', { blocks: [{ type: 'tool', name: 'Bash' }] })], { offset: 10 })
  assert.equal(partial.truncated, true)
  assert.equal(footerHasCopy(partial), false, 'unknown until its start is read')
  assert.equal(footerHasCopy(partial, { startRows: [row('earlier')] }), true, 'the read start has prose')
  // Whether Copy shows agrees with the text it builds when pressed.
  for (const footer of [prose, tools]) {
    assert.equal(footerHasCopy(footer), (footerCopyParts(footer, { messages: [] }) || []).length > 0)
  }
})

test('Copy’s parts come from the stretch’s replies as they render, each naming its row', () => {
  const messages = [ownerRow, row('a'), cardAnswer, failedReply(
    { type: 'text', content: 'Usage limit reached' }, { type: 'error', message: 'Usage limit reached' })]
  const footer = footerOf(messages)
  assert.deepEqual(stretchProse(footer.groups), [{ block: 0, markdown: 'a', key: 'a' }],
    'an error echo is the error, so a stretch ending on one copies only what came before')
  assert.deepEqual(stretchProse(footerOf([ownerRow, row('tools', { blocks: [{ type: 'tool', name: 'Bash' }] })]).groups), [],
    'nothing to copy, so no Copy')
})

test('the footer reads when the reply’s run ended, or says it is the start', () => {
  const steer = { role: 'user', hidden: true, steered: true, source_work_id: 'run', kind: 'peer_message' }
  const ended = Date.UTC(2026, 9, 22, 7, 5)
  const footerTime = messages => {
    const group = assistantReplyGroups(messages).get(0)
    return actionRow(renderWithModels(createElement(PeerTimelineContext.Provider,
      { value: { tools: new Map(), positions: new Map() } },
      createElement(Reply, {
        replyGroup: group, activeRowIndex: -1, activeMirrorMsg: group.rows.at(-1).message,
        useDbActivePayload: true, chatId: 'fixture', footer: 'settled', stretchSourceRows: [],
      }))))
  }
  assert.match(footerTime([row('run'), steer, row('run:assistant:1', { run_ended_ts: ended })]),
    /<time class="chat__ts" title="22nd Oct 2026, \d\d:\d\d">22nd Oct 2026, /,
    'the server’s end of the run that wrote the last row')
  assert.match(footerTime([row('run'), steer, row('run:assistant:1')]),
    /<time class="chat__ts" title="Started 21st Oct 2026, \d\d:\d\d">Started 21st Oct 2026, /,
    'without it, the reply’s start, named as such')
})

test('a stretch whose start was read copies as the same stretch wholly loaded, peer notes included', () => {
  // One run split by a hidden steer whose seam carries a peer note: the note
  // keeps the replay from joining, so the parts depend on the note slots.
  const steer = { role: 'user', hidden: true, steered: true, source_work_id: 'run', kind: 'peer_message' }
  const sealed = { role: 'assistant', id: 'run', ts: turn.ts, content: 'The frame', blocks: [{ type: 'text', content: 'The frame' }] }
  const continuation = { role: 'assistant', id: 'run:assistant:1', ts: turn.ts, blocks: [{ type: 'text', content: 'The framework survives.' }] }
  const note = [{ id: 'note', type: 'peer_message' }]
  const partsOf = (messages, { slots = new Map(), offset = 0, startRows } = {}) => {
    const replyGroups = assistantReplyGroups(messages, { slots, offset })
    const lastVisibleIndex = messages.findLastIndex(message => !message.hidden)
    const footer = replyFooters({ messages, replyGroups, lastVisibleIndex, offset }).current
    return footerCopyParts(footer, { startRows, messages, offset, slots })
  }
  const loaded = [ownerRow, sealed, steer, continuation]
  const whole = partsOf(loaded, { slots: new Map([[2, note]]) })
  const page = [steer, continuation]
  assert.equal(partsOf(page, { offset: 2, slots: new Map([[0, note]]) }), null,
    'unknown until the start is read, so no Copy yet')
  assert.deepEqual(partsOf(page, { offset: 2, slots: new Map([[0, note]]), startRows: [sealed] }), whole,
    'the read start joins the loaded rows with the transcript’s own note slots')
  assert.notDeepEqual(partsOf(loaded), whole, 'the note does change how the reply reads')
})

test('Copy has no parts until the footer settles', () => {
  const messages = [ownerRow, row('a')]
  const footer = footerOf(messages)
  assert.equal(footerCopyParts({ ...footer, state: 'working' }, { messages }), null)
  assert.equal(footerCopyParts(null, { messages }), null)
})

test('every action row reads time, then Copy, then Branch', () => {
  for (const side of ['end', 'start']) {
    const html = renderWithModels(createElement(MetaRow, {
      timestamp: turn.ts, copy: () => null, branch: () => {}, side,
    }))
    const at = needle => html.indexOf(needle)
    assert.ok(at('<time') >= 0 && at('<time') < at('aria-label="Copy message"')
      && at('aria-label="Copy message"') < at('aria-label="Branch in new chat"'), side)
  }
})
