/**
 * Pure-function tests for streamReducers.js — the live-stream merge
 * policy for AskUserQuestion (question events absorbing their own
 * tool block in place, answer-carry on re-delivery, and tool_output/
 * tool_end routing around absorbed cards).
 *
 * These import the REAL module (not a simulation) so the tests fail
 * when the reducer drifts.
 *
 * Run with:
 *   cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/components/ChatView/__tests__/streamReducers.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  upsertQuestionItem,
  attachToolOutput,
  closeToolLifecycle,
  closeAllToolLifecycles,
  isQuestionTool,
  suppressedQuestionToolIndices,
} from '../streamReducers.js'
import { questionKey } from '../questionKey.js'

// The exact item shape useStreamConnection's tool_start handler appends.
function toolItem(tool, overrides = {}) {
  return { type: 'tool', tool, input: '', output: '', status: 'running', ...overrides }
}

function questionEvent(id, text) {
  return {
    type: 'question',
    question_id: id,
    questions: [{ question: text, options: [{ label: 'A' }, { label: 'B' }] }],
  }
}

// ---------------------------------------------------------------------------
// upsertQuestionItem — the card replaces its own pending tool block
// ---------------------------------------------------------------------------

test('question replaces the running AskUserQuestion tool item at its position', () => {
  const prev = [
    { type: 'text', content: 'Let me ask you something.' },
    toolItem('AskUserQuestion', { input: 'Pick a color' }),
  ]
  const next = upsertQuestionItem(prev, questionEvent('q1', 'Pick a color'))
  assert.equal(next.length, 2, 'tool item replaced, not appended alongside')
  assert.equal(next[0].type, 'text')
  assert.equal(next[1].type, 'question', 'card takes the tool block position')
  assert.equal(next[1].question_id, 'q1')
  assert.equal(next[1].absorbedTool, 'AskUserQuestion')
  assert.equal(next.filter(it => it.type === 'tool').length, 0)
})

test('question replaces a running request_user_input tool item (Codex name)', () => {
  const prev = [toolItem('request_user_input')]
  const next = upsertQuestionItem(prev, questionEvent('q1', 'Pick one'))
  assert.equal(next.length, 1)
  assert.equal(next[0].type, 'question')
  assert.equal(next[0].absorbedTool, 'request_user_input')
})

test('question with no preceding tool item appends (Codex publishes no tool_start today)', () => {
  const prev = [{ type: 'text', content: 'hello' }]
  const next = upsertQuestionItem(prev, questionEvent('q1', 'Pick one'))
  assert.equal(next.length, 2)
  assert.equal(next[1].type, 'question')
  assert.equal(next[1].absorbedTool, undefined, 'nothing absorbed when nothing pending')
})

test('a running NON-question tool is never absorbed by a question', () => {
  const prev = [toolItem('Bash', { input: 'ls' })]
  const next = upsertQuestionItem(prev, questionEvent('q1', 'Pick one'))
  assert.equal(next.length, 2, 'appended — Bash block untouched')
  assert.equal(next[0].type, 'tool')
  assert.equal(next[0].tool, 'Bash')
  assert.equal(next[0].status, 'running')
})

test('a DONE question-tool item is not absorbed (lifecycle already closed)', () => {
  const prev = [toolItem('AskUserQuestion', { status: 'done', output: 'answered earlier' })]
  const next = upsertQuestionItem(prev, questionEvent('q2', 'Second question'))
  assert.equal(next.length, 2, 'appended after the closed tool block')
  assert.equal(next[0].status, 'done')
  assert.equal(next[1].type, 'question')
})

// ---------------------------------------------------------------------------
// upsertQuestionItem — same-key re-delivery merges in place (the
// persisted-history answer-carry applied to the live reducer)
// ---------------------------------------------------------------------------

test('same-key re-delivery updates the card in place and keeps its position', () => {
  const prev = [
    questionEvent('q1', 'Pick a color'),
    { type: 'text', content: 'while you decide...' },
  ]
  const grown = {
    ...questionEvent('q1', 'Pick a color'),
    questions: [
      { question: 'Pick a color', options: [{ label: 'A' }, { label: 'B' }, { label: 'C' }] },
    ],
  }
  const next = upsertQuestionItem(prev, grown)
  assert.equal(next.length, 2, 'no duplicate card')
  assert.equal(next[0].type, 'question', 'position preserved')
  assert.equal(next[0].questions[0].options.length, 3, 'incoming questions win')
})

test('re-delivered question event without answers cannot re-arm an answered card', () => {
  // patchQuestionAnswers' optimistic update shape: { ...item, answers }.
  const answered = { ...questionEvent('q1', 'Pick a color'), answers: { 'Pick a color': 'A' } }
  const next = upsertQuestionItem([answered], questionEvent('q1', 'Pick a color'))
  assert.equal(next.length, 1)
  assert.deepEqual(next[0].answers, { 'Pick a color': 'A' }, 'answers carried over')
})

test('same-key re-delivery preserves an open absorbed-tool lifecycle', () => {
  const prev = upsertQuestionItem(
    [toolItem('AskUserQuestion')], questionEvent('q1', 'Pick a color'),
  )
  const next = upsertQuestionItem(prev, questionEvent('q1', 'Pick a color'))
  assert.equal(next.length, 1)
  assert.equal(next[0].absorbedTool, 'AskUserQuestion',
    'tool lifecycle still open after re-delivery')
})

// ---------------------------------------------------------------------------
// attachToolOutput / closeToolLifecycle — routing around absorbed cards
// ---------------------------------------------------------------------------

test('tool_output still attaches to a running ordinary tool (non-regression)', () => {
  const prev = [toolItem('Bash', { input: 'ls' })]
  const next = attachToolOutput(prev, 'file1\nfile2')
  assert.equal(next[0].output, 'file1\nfile2')
  assert.equal(next[0].status, 'running', 'output does not close the lifecycle')
})

test('post-answer tool_output is swallowed by the absorbed card, not appended', () => {
  const items = upsertQuestionItem(
    [toolItem('AskUserQuestion')], questionEvent('q1', 'Pick a color'),
  )
  const next = attachToolOutput(items, 'Your questions have been answered: "Pick a color"="A"')
  assert.equal(next.length, 1, 'no new block for the answers echo')
  assert.equal(next[0].type, 'question')
  assert.equal(next[0].output, undefined, 'card never grows a tool output field')
})

test('post-answer tool_output cannot corrupt an EARLIER running tool block', () => {
  // The old reverse-search-for-running-tool would have attached the
  // answers echo to the stuck Bash block once the question replaced
  // its own tool item. The absorbed card is the open lifecycle and
  // must win.
  const stuck = toolItem('Bash', { input: 'long-running' })
  const items = upsertQuestionItem(
    [stuck, toolItem('AskUserQuestion')], questionEvent('q1', 'Pick a color'),
  )
  const next = attachToolOutput(items, 'Your questions have been answered: ...')
  assert.equal(next[0].output, '', 'Bash output untouched')
})

test('tool_end closes the absorbed lifecycle by dropping absorbedTool', () => {
  const items = upsertQuestionItem(
    [toolItem('AskUserQuestion')], questionEvent('q1', 'Pick a color'),
  )
  const next = closeToolLifecycle(items)
  assert.equal(next.length, 1)
  assert.equal(next[0].type, 'question')
  assert.equal('absorbedTool' in next[0], false, 'lifecycle marker removed')
})

test('tool_end after absorption does not flip an earlier running tool to done', () => {
  const stuck = toolItem('Bash', { input: 'long-running' })
  const items = upsertQuestionItem(
    [stuck, toolItem('AskUserQuestion')], questionEvent('q1', 'Pick a color'),
  )
  const next = closeToolLifecycle(items)
  assert.equal(next[0].status, 'running', 'Bash lifecycle untouched')
  assert.equal('absorbedTool' in next[1], false)
})

test('tool_end without absorption flips the last running tool to done (non-regression)', () => {
  const prev = [toolItem('Bash', { status: 'done' }), toolItem('Grep')]
  const next = closeToolLifecycle(prev)
  assert.equal(next[1].status, 'done')
  assert.equal(next[0].status, 'done', 'earlier block untouched')
})

test('closed absorbed card no longer attracts later tool events', () => {
  let items = upsertQuestionItem(
    [toolItem('AskUserQuestion')], questionEvent('q1', 'Pick a color'),
  )
  items = closeToolLifecycle(items)
  // A second tool starts after the question resolved.
  items = [...items, toolItem('Bash', { input: 'echo hi' })]
  items = attachToolOutput(items, 'hi')
  assert.equal(items[1].output, 'hi', 'output goes to the new running tool')
  assert.equal(items[0].output, undefined)
})

// ---------------------------------------------------------------------------
// closeAllToolLifecycles — provider-error sweep
// ---------------------------------------------------------------------------

test('error sweep closes running tools AND absorbed-card lifecycles', () => {
  const items = upsertQuestionItem(
    [toolItem('Bash'), toolItem('AskUserQuestion')],
    questionEvent('q1', 'Pick a color'),
  )
  const next = closeAllToolLifecycles(items)
  assert.equal(next[0].status, 'done', 'running tool flipped')
  assert.equal('absorbedTool' in next[1], false, 'absorbed lifecycle closed')
  assert.equal(next[1].type, 'question', 'card survives the sweep')
})

// ---------------------------------------------------------------------------
// Full wire-order integration: the exact Claude SDK event sequence for
// an answered AskUserQuestion turn (verified against a live SDK probe
// and prod persisted block order):
//   tool_start → tool_input → question → [answer] → tool_output → tool_end
// ---------------------------------------------------------------------------

test('full question turn renders exactly ONE block, answered in place', () => {
  let items = [{ type: 'text', content: 'Quick question first.' }]

  // tool_start (useStreamConnection appends this shape verbatim)
  items = [...items, toolItem('AskUserQuestion')]
  // tool_input backfill (earliest tool without input)
  items = items.map(it =>
    it.type === 'tool' && !it.input ? { ...it, input: 'Pick a color' } : it)
  // question event — absorbs the tool block in place
  items = upsertQuestionItem(items, questionEvent('q1', 'Pick a color'))
  assert.equal(items.length, 2, 'pending: text + ONE card, no tool twin')
  assert.equal(items[1].type, 'question')

  // user answers — patchQuestionAnswers' optimistic shape
  items = items.map(it =>
    it.type === 'question' ? { ...it, answers: { 'Pick a color': 'A' } } : it)
  // post-answer echo + lifecycle close
  items = attachToolOutput(items, 'Your questions have been answered: "Pick a color"="A"')
  items = closeToolLifecycle(items)
  // post-answer assistant text resumes
  items = [...items, { type: 'text', content: 'A it is.' }]

  assert.equal(items.length, 3, 'text + answered card + text')
  assert.equal(items[1].type, 'question', 'card kept the tool block position')
  assert.deepEqual(items[1].answers, { 'Pick a color': 'A' })
  assert.equal('absorbedTool' in items[1], false)
  assert.equal(items.filter(it => it.type === 'tool').length, 0,
    'the AskUserQuestion tool twin never renders')
})

// ---------------------------------------------------------------------------
// isQuestionTool
// ---------------------------------------------------------------------------

test('isQuestionTool matches exactly the two question-tool names', () => {
  assert.ok(isQuestionTool('AskUserQuestion'))
  assert.ok(isQuestionTool('request_user_input'))
  assert.equal(isQuestionTool('Bash'), false)
  assert.equal(isQuestionTool('Skill'), false)
})

// ---------------------------------------------------------------------------
// suppressedQuestionToolIndices — persisted-reopen tool-twin suppression
// (MsgContent renders persisted blocks; the backend keeps the raw
// AskUserQuestion tool block AND the question card, the live stream
// absorbs the twin — this helper hides the twin at render time)
// ---------------------------------------------------------------------------

// The exact persisted shape: events.process_event appends a `tool` block
// from tool_start, then a separate `question` block from the question event.
function persistedQuestionBlocks(toolName = 'AskUserQuestion') {
  return [
    { type: 'text', content: 'Quick question first.' },
    { type: 'tool', tool: toolName, input: 'Pick a color', output: '', status: 'done' },
    { type: 'question', question_id: 'q1', questions: [{ question: 'Pick a color' }],
      answers: { 'Pick a color': 'A' } },
    { type: 'text', content: 'A it is.' },
  ]
}

test('suppresses the AskUserQuestion tool twin when a question card is present', () => {
  const blocks = persistedQuestionBlocks()
  const skip = suppressedQuestionToolIndices(blocks)
  assert.ok(skip.has(1), 'the tool twin at index 1 is suppressed')
  assert.equal(skip.size, 1, 'only the tool twin is suppressed')
  assert.equal(skip.has(2), false, 'the question card itself is never suppressed')
})

test('suppresses the Codex request_user_input tool twin too', () => {
  const blocks = persistedQuestionBlocks('request_user_input')
  const skip = suppressedQuestionToolIndices(blocks)
  assert.ok(skip.has(1))
})

test('does NOT suppress a question-tool block when no question card is present', () => {
  // Defensive edge: a lone AskUserQuestion tool block with no card. Leave
  // it visible rather than hide a tool the user has no other view into.
  const blocks = [
    { type: 'tool', tool: 'AskUserQuestion', input: 'x', output: '', status: 'done' },
  ]
  assert.equal(suppressedQuestionToolIndices(blocks).size, 0)
})

test('does NOT suppress ordinary tool blocks in a question message', () => {
  const blocks = [
    { type: 'tool', tool: 'Bash', input: 'ls', output: 'a', status: 'done' },
    { type: 'tool', tool: 'AskUserQuestion', input: 'Pick', output: '', status: 'done' },
    { type: 'question', question_id: 'q1', questions: [{ question: 'Pick' }] },
  ]
  const skip = suppressedQuestionToolIndices(blocks)
  assert.equal(skip.has(0), false, 'Bash tool stays visible')
  assert.ok(skip.has(1), 'AskUserQuestion twin suppressed')
})

test('suppresses every AskUserQuestion twin when two questions are in one message', () => {
  const blocks = [
    { type: 'tool', tool: 'AskUserQuestion', input: 'Q1', output: '', status: 'done' },
    { type: 'question', question_id: 'q1', questions: [{ question: 'Q1' }] },
    { type: 'tool', tool: 'AskUserQuestion', input: 'Q2', output: '', status: 'done' },
    { type: 'question', question_id: 'q2', questions: [{ question: 'Q2' }] },
  ]
  const skip = suppressedQuestionToolIndices(blocks)
  assert.deepEqual([...skip].sort(), [0, 2])
})

test('suppressedQuestionToolIndices tolerates non-array input', () => {
  assert.equal(suppressedQuestionToolIndices(undefined).size, 0)
  assert.equal(suppressedQuestionToolIndices(null).size, 0)
})

// ---------------------------------------------------------------------------
// Reconnect answer-survival — the answersByQuestionKey re-arming contract.
// useStreamConnection keeps a key→answers map (written by
// patchQuestionAnswers) that outlives the streamItems wipe every reconnect
// performs; the `question` handler re-arms each incoming event from it
// before upsertQuestionItem runs. This simulates that exact sequence
// against the REAL upsertQuestionItem so a regression in the carry breaks.
// ---------------------------------------------------------------------------

test('a reconnect wipe + catch-up replay keeps the answered state (re-arm path)', () => {
  // Turn opens, the card arrives and absorbs its tool twin.
  let items = upsertQuestionItem([toolItem('AskUserQuestion')], questionEvent('q1', 'Pick a color'))
  // User answers — patchQuestionAnswers records the answer in the map AND
  // patches the live item.
  const answersByKey = new Map()
  const answers = { 'Pick a color': 'A' }
  answersByKey.set('question_id:q1', answers)
  items = items.map(it => it.type === 'question' ? { ...it, answers } : it)
  assert.deepEqual(items[0].answers, answers, 'card answered before reconnect')

  // RECONNECT: every path wipes streamItems before the catch-up burst.
  items = []

  // Catch-up replays the original question event WITHOUT answers. The hook
  // re-arms it from the surviving map BEFORE upsertQuestionItem runs.
  const incoming = { type: 'question', question_id: 'q1', questions:
    [{ question: 'Pick a color', options: [{ label: 'A' }, { label: 'B' }] }] }
  const known = answersByKey.get(questionKey(incoming))
  if (known && !incoming.answers) incoming.answers = known
  items = upsertQuestionItem(items, incoming)

  assert.equal(items.length, 1, 'one card after replay')
  assert.deepEqual(items[0].answers, answers,
    'card stays ANSWERED across the reconnect instead of reverting to pending')
})

test('without the surviving map, a wiped replay reverts to pending (proves the bug)', () => {
  // This is the pre-fix behavior: after the wipe upsertQuestionItem has no
  // prior item to carry answers from, and the replay carries none.
  let items = []
  const incoming = questionEvent('q1', 'Pick a color')
  items = upsertQuestionItem(items, incoming)
  assert.equal(items[0].answers, undefined,
    'a bare replay over wiped items has no answers — the exact revert the map fixes')
})
