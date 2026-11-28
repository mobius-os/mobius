/* A request-list card the agent carried on past with nothing answered leaves
 * an "Asked a question" activity row where it was, rendered and grouped like
 * any other tool call; a card with answers stays a card. */
import { after, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

globalThis.location = { origin: 'http://localhost' }
globalThis.window = { location: globalThis.location, innerHeight: 800 }
const values = new Map()
globalThis.sessionStorage = {
  getItem: key => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: key => values.delete(key),
}

const vite = await createSsrRenderServer({
  noExternal: ['dompurify'],
  plugins: [{
    name: 'dompurify-ssr-passthrough', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-passthrough' : null,
    load: id => id === '\0dompurify-passthrough' ? 'export default { sanitize: value => String(value) }' : null,
  }],
})
after(() => vite.close())
const { default: MsgContent } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const {
  askedQuestionsActivity, askedQuestionsLabel, cardLeavesActivityRow, ASKED_QUESTIONS_TOOL,
} = await vite.ssrLoadModule('/src/components/ChatView/askedQuestionsActivity.js')
const { toolCallLabel, effectiveToolName, isDistinctiveActivityTool } = await vite.ssrLoadModule('/src/components/ChatView/toolActivityLabel.js')
const { groupActivityRuns } = await vite.ssrLoadModule('/src/components/ChatView/activityGrouping.js')
const { activityCollapsedLabel } = await vite.ssrLoadModule('/src/components/ChatView/groupBlocks.js')
const { persistDisclosureOpen, _resetDisclosureStateForTests } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')

beforeEach(() => { values.clear(); _resetDisclosureStateForTests() })

const tone = { id: 'r-tone', kind: 'question', header: 'Protected folder', question: 'How formal?', options: [] }
const publish = {
  id: 'r-publish', kind: 'approval', header: 'Approval', question: 'Publish the notes?',
  options: [{ id: '0', label: 'Publish', description: 'Post them now.' }, { id: '1', label: 'Not now' }],
}
const secret = {
  id: 'r-secret', kind: 'secret', question: 'Mail key', options: [],
  secure_input: { title: 'Connect the mail service', fields: [{ name: 'api_key', label: 'API key' }] },
}
const listCard = (extra = {}) => ({
  type: 'question', question_id: 'card-1', input_requests: true, questions: [tone, publish, secret], ...extra,
})
const reply = card => ({ role: 'assistant', id: 'reply', blocks: [{ type: 'text', content: 'Three things need you.' }, card] })
const render = (msg, extra = {}) => renderToStaticMarkup(createElement(MsgContent, {
  msg, chatId: 'chat-1', messageKey: 'reply', isLastMsg: true, liveQuestionId: null,
  onQuestionAnswer: () => true, ...extra,
}))

test('only an unanswered list card the owner can no longer answer becomes the activity row', () => {
  assert.equal(cardLeavesActivityRow(listCard()), true)
  assert.equal(cardLeavesActivityRow(listCard(), { answerable: true }), false)
  assert.equal(cardLeavesActivityRow(listCard(), { locallyAnswered: true }), false, 'an answer on its way keeps the card')
  assert.equal(cardLeavesActivityRow(listCard({ answers: { 'How formal?': 'Casual' } })), false)
  assert.equal(cardLeavesActivityRow(listCard({ request_states: { 'r-secret': { status: 'completed' } } })), false)
  assert.equal(cardLeavesActivityRow({ ...listCard(), input_requests: undefined }), false, 'a legacy card is never traced')
})

test('the row reads "Asked a question" or "Asked questions", never what went unanswered', () => {
  const row = askedQuestionsActivity(listCard())
  assert.equal(row.type, 'tool')
  assert.equal(effectiveToolName(row), ASKED_QUESTIONS_TOOL)
  assert.equal(isDistinctiveActivityTool(row), false, 'it folds with neighbouring activity')
  assert.equal(toolCallLabel(row), 'Asked questions')
  assert.equal(askedQuestionsLabel(askedQuestionsActivity(listCard({ questions: [tone] }))), 'Asked a question')
  assert.equal(activityCollapsedLabel([{ item: askedQuestionsActivity(listCard({ questions: [tone] })), idx: 1 }]), 'Asked a question')
  assert.deepEqual(row.asked.map(entry => [entry.label, entry.prompt]), [
    ['', 'How formal?'], ['Approval', 'Publish the notes?'], ['Secure input', 'Connect the mail service'],
  ])
  assert.doesNotMatch(JSON.stringify(row), /not answered|unanswered|Protected folder|api_key/)
})

test('a card the agent carried on past renders as a tool row where the card was, folded until opened', () => {
  const html = render(reply(listCard()))
  assert.doesNotMatch(html, /class="qcard/)
  const prose = html.indexOf('Three things need you.')
  const row = html.indexOf('<div class="chat__tool chat__tool--done chat__tool--compact">')
  assert.ok(prose >= 0 && row > prose, html)
  assert.match(html, /<button[^>]*class="chat__tool-header" aria-expanded="false"[^>]*>.*<span class="chat__tool-name" title="Asked questions">Asked questions<\/span><\/button>/)
  assert.match(html, /class="chat__tool-detail chat__asked-detail"[^>]*hidden=""><\/div>/, 'nothing shown until the owner opens it')
  assert.doesNotMatch(html, /not answered|<textarea|<input|role="radio"/)

  persistDisclosureOpen('chat-1', 'reply:tool:owner-card:card-1', true)
  _resetDisclosureStateForTests()
  const opened = render(reply(listCard()))
  assert.match(opened, /aria-expanded="true"/)
  for (const text of ['How formal?', 'Approval', 'Publish the notes?', 'Post them now.', 'Not now', 'Connect the mail service']) {
    assert.ok(opened.includes(text), text)
  }
  assert.doesNotMatch(opened, /<textarea|<input|role="radio"|<form|Submit/, 'read-only')
})

test('it folds into the activity run beside other tool calls', () => {
  const nodes = groupActivityRuns([
    { item: { type: 'tool', tool: 'Bash', tool_use_id: 'b1', input: 'npm test', status: 'done' }, idx: 0 },
    { item: askedQuestionsActivity(listCard()), idx: 1 },
  ])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].group.length, 2)
})

test('an answerable card, an answered card and a card with a settled entry stay cards', () => {
  const open = render(reply(listCard()), { liveQuestionId: 'card-1' })
  assert.match(open, /class="qcard qcard--grouped"/)
  // Every card renders in the full-width owner-card row.
  assert.match(open, /<div class="chat__owner-card">/)
  const answered = render(reply(listCard({ answers: { 'How formal?': 'Casual' } })))
  assert.match(answered, /class="qcard qcard--grouped qcard--answered"|class="qcard qcard--answered"/)
  assert.doesNotMatch(answered, /Asked a question|Asked questions|not answered/, 'nothing about the entries left')
  assert.doesNotMatch(answered, /Publish the notes\?/, 'unanswered entries are not listed on the card')
  const settled = render(reply(listCard({ request_states: { 'r-secret': { status: 'completed' } } })))
  assert.match(settled, /class="qcard/)
})

test('an old card naming one request by input_request_id renders as ordinary history', () => {
  const approval = { id: 'approval', header: 'Approval', question: 'Publish?', options: [{ id: '0', label: 'Publish' }, { id: '1', label: 'Not now' }] }
  const ordinary = extra => ({ type: 'question', question_id: 'old-card', response_mode: 'continuation', questions: [approval], ...extra })
  const old = extra => ordinary({ input_request_id: 'request-1', input_request_revision: 1, ...extra })
  const answered = { answers: { 'Publish?': 'Not now' }, selected_options: { approval: ['1'] } }

  const answeredHtml = render(reply(old(answered)))
  assert.equal(answeredHtml, render(reply(ordinary(answered))))
  assert.match(answeredHtml, /class="qcard qcard--answered"/)
  assert.match(answeredHtml, /aria-checked="true"[^>]*><span[^>]*><\/span><span class="qcard__opt-label">Not now</)

  // Superseded before it was answered: a closed card, never an activity row.
  const supersededHtml = render(reply(old()))
  assert.equal(supersededHtml, render(reply(ordinary())))
  assert.match(supersededHtml, /class="qcard" [^>]*aria-disabled="true"/)
  assert.doesNotMatch(supersededHtml, /Asked a question|Asked questions/)
})
