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
  appendThinkingChunk,
  anchorReplayedThinking,
  thinkingContentForDisplay,
  thinkingElapsedMs,
  attachToolSources,
  reconcileStreamItems,
  appendTextItem,
  repairInterleavedQuestionText,
  replaceTextItem,
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

test('text item identity keeps late deltas before an interleaved question', () => {
  let items = appendTextItem([], 'Build it', { textItemId: 'msg-1' })
  items = upsertQuestionItem(items, questionEvent('q1', 'Proceed?'))
  items = appendTextItem(items, ' safely', { textItemId: 'msg-1' })
  items = replaceTextItem(items, 'Build it safely', { textItemId: 'msg-1' })

  assert.deepEqual(items.map(item => item.type), ['text', 'question'])
  assert.equal(items[0].content, 'Build it safely')
  assert.equal(items[1].question_id, 'q1')
})

test('legacy full text repairs prefix-question-suffix catch-up order', () => {
  const items = replaceTextItem([
    { type: 'text', content: 'Build it' },
    questionEvent('q1', 'Proceed?'),
    { type: 'text', content: ' safely' },
  ], 'Build it safely')

  assert.deepEqual(items.map(item => item.type), ['text', 'question'])
  assert.equal(items[0].content, 'Build it safely')
})

test('full text with mismatched identity stays before an unanswered question', () => {
  const items = replaceTextItem([
    { type: 'text', content: 'Review res', text_item_id: 'delta-item' },
    questionEvent('q1', 'Harden it?'),
  ], 'Review result', { textItemId: 'completed-item' })

  assert.deepEqual(items.map(item => item.type), ['text', 'question'])
  assert.equal(items[0].content, 'Review result')
  assert.equal(items[1].question_id, 'q1')
})

test('persisted prefix-question-full duplication is repaired for display', () => {
  const stored = [
    { type: 'text', content: 'Review res' },
    questionEvent('q1', 'Harden it?'),
    { type: 'text', content: 'Review result' },
  ]
  const repaired = repairInterleavedQuestionText(stored)

  assert.deepEqual(repaired.map(item => item.type), ['text', 'question'])
  assert.equal(repaired[0].content, 'Review result')
  assert.equal(stored.length, 3, 'the stored transcript is not mutated')
})

test('answered question boundaries preserve deliberate later text', () => {
  const stored = [
    { type: 'text', content: 'Before' },
    { ...questionEvent('q1', 'Proceed?'), answers: { 'Proceed?': 'Yes' } },
    { type: 'text', content: 'Before and after' },
  ]

  assert.equal(repairInterleavedQuestionText(stored), stored)
})

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

test('batched tool output and end resolve by id, not trailing position', () => {
  const prev = [
    toolItem('WebSearch', { tool_use_id: 'first' }),
    toolItem('WebSearch', { tool_use_id: 'second' }),
  ]
  const withOutput = attachToolOutput(prev, 'first result', {
    tool_use_id: 'first',
  })
  const closed = closeToolLifecycle(withOutput, 'first')
  assert.equal(closed[0].output, 'first result')
  assert.equal(closed[0].status, 'done')
  assert.equal(closed[1].output, '')
  assert.equal(closed[1].status, 'running')
})

test('an unknown explicit tool id never corrupts another open tool', () => {
  const prev = [
    toolItem('Bash', { tool_use_id: 'a' }),
    toolItem('Bash', { tool_use_id: 'b' }),
  ]
  assert.equal(attachToolOutput(prev, 'wrong', {
    tool_use_id: 'missing',
  }), prev)
  assert.equal(closeToolLifecycle(prev, 'missing'), prev)
})

test('large live tool output keeps the metadata needed for lazy full fetch', () => {
  const prev = [toolItem('Bash', { input: 'cat big.log' })]
  const next = attachToolOutput(prev, 'bounded excerpt', {
    tool_use_id: 'toolu_large',
    output_truncated: true,
    output_full_len: 120_000,
    output_exit_code: 0,
  })
  assert.deepEqual(next[0], {
    ...prev[0],
    output: 'bounded excerpt',
    tool_use_id: 'toolu_large',
    output_truncated: true,
    output_full_len: 120_000,
    output_exit_code: 0,
  })
})

test('live output metadata cannot replace a runner-assigned tool identity', () => {
  const prev = [toolItem('Bash', { tool_use_id: 'toolu_original' })]
  const next = attachToolOutput(prev, 'bounded excerpt', {
    tool_use_id: 'toolu_late',
    output_truncated: true,
    output_full_len: 9_000,
  })
  assert.equal(next[0].tool_use_id, 'toolu_original')
})

test('tool_sources attach to the latest WebSearch tool block', () => {
  const sources = [{ title: 'Docs', url: 'https://example.com/docs' }]
  const prev = [
    toolItem('WebSearch', { status: 'done', sources: [{ url: 'old' }] }),
    toolItem('Bash', { input: 'ls' }),
    toolItem('WebSearch', { input: 'docs' }),
  ]
  const next = attachToolSources(prev, sources)
  assert.deepEqual(next[0].sources, [{ url: 'old' }])
  assert.deepEqual(next[2].sources, sources)
})

// A turn can run several WebSearch calls in ONE batch, so every tool_sources
// event arrives while the LAST search item is trailing. Matching by position
// alone landed them all on that one item, each overwriting the previous, and
// only the final search's sources survived.
test('batched WebSearch calls each keep their own sources (matched by id)', () => {
  const prev = [
    toolItem('WebSearch', { input: 'query A', tool_use_id: 'toolu_a' }),
    toolItem('WebSearch', { input: 'query B', tool_use_id: 'toolu_b' }),
  ]
  const a = [{ title: 'A', url: 'https://a.example/1' }]
  const b = [{ title: 'B', url: 'https://b.example/2' }]

  const next = attachToolSources(attachToolSources(prev, a, 'toolu_a'), b, 'toolu_b')

  assert.deepEqual(next[0].sources, a, "the first search keeps its own results")
  assert.deepEqual(next[1].sources, b)
})

test('replaying the same tool_sources event does not duplicate (catch-up safe)', () => {
  const prev = [toolItem('WebSearch', { tool_use_id: 'toolu_a' })]
  const sources = [{ title: 'A', url: 'https://a.example/1' }]
  const once = attachToolSources(prev, sources, 'toolu_a')
  const twice = attachToolSources(once, sources, 'toolu_a')
  assert.deepEqual(twice[0].sources, sources)
})

test('a later source event enriches a URL-only result in place', () => {
  const url = 'https://a.example/1'
  const prev = [toolItem('WebSearch', { tool_use_id: 'toolu_a' })]
  const weak = attachToolSources(prev, [{ title: url, url }], 'toolu_a')
  const rich = attachToolSources(weak, [{
    title: 'A', url, snippet: 'context',
  }], 'toolu_a')
  assert.deepEqual(rich[0].sources, [{ title: 'A', url, snippet: 'context' }])
})

test('an explicit id with no matching item never misattributes sources', () => {
  const prev = [toolItem('WebSearch', { input: 'only' })]
  const sources = [{ title: 'A', url: 'https://a.example/1' }]
  const next = attachToolSources(prev, sources, 'toolu_missing')
  assert.equal(next, prev)
  assert.equal(next[0].sources, undefined)
})

test('a source id cannot attach metadata to a non-WebSearch tool', () => {
  const prev = [
    toolItem('WebSearch', { tool_use_id: 'search-a' }),
    toolItem('Bash', { tool_use_id: 'bash-a' }),
  ]
  const sources = [{ title: 'A', url: 'https://a.example/1' }]
  const next = attachToolSources(prev, sources, 'bash-a')
  assert.equal(next, prev)
  assert.equal(next[0].sources, undefined)
  assert.equal(next[1].sources, undefined)
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

test('an absorbed question keeps its tool id for exact output/end routing', () => {
  const items = upsertQuestionItem(
    [toolItem('AskUserQuestion', { tool_use_id: 'question-tool' })],
    questionEvent('q1', 'Pick a color'),
  )
  assert.equal(items[0].absorbedToolUseId, 'question-tool')
  const afterOutput = attachToolOutput(items, 'answered', {
    tool_use_id: 'question-tool',
  })
  const closed = closeToolLifecycle(afterOutput, 'question-tool')
  assert.equal('absorbedTool' in closed[0], false)
  assert.equal('absorbedToolUseId' in closed[0], false)
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

test('appendThinkingChunk coalesces consecutive deltas into one item', () => {
  let items = []
  items = appendThinkingChunk(items, 'Let me ', 1000, 10000)
  items = appendThinkingChunk(items, 'think about ', 1750, 10750)
  items = appendThinkingChunk(items, 'this.', 2600, 11600)
  assert.equal(items.length, 1, 'consecutive thinking deltas stay one item')
  assert.deepEqual(items[0], {
    type: 'thinking',
    content: 'Let me think about this.',
    startedAt: 1000,
    firstTs: 10000,
    lastAt: 2600,
    duration_ms: 1600,
  })
})

test('appendThinkingChunk separates provider reasoning segments but not token deltas', () => {
  let items = []
  items = appendThinkingChunk(items, '**Planning ', 1000, 10000, 'summary:0')
  items = appendThinkingChunk(items, 'the fix**', 1100, 10100, 'summary:0')
  items = appendThinkingChunk(items, '**Writing tests**', 1200, 10200, 'summary:1')
  assert.equal(items.length, 1)
  assert.equal(items[0].content, '**Planning the fix**\n\n**Writing tests**')
  assert.equal(items[0].segmentId, 'summary:1')
})

test('thinkingContentForDisplay repairs legacy glued bold summary headings', () => {
  assert.equal(
    thinkingContentForDisplay('**Planning****Testing****Finishing**'),
    '**Planning**\n\n**Testing**\n\n**Finishing**',
  )
  assert.equal(thinkingContentForDisplay('token fragments'), 'token fragments')
})

test('appendThinkingChunk keeps replay duration from runner timestamps', () => {
  let items = []
  items = appendThinkingChunk(items, 'Long ', 1000, 10000)
  items = appendThinkingChunk(items, 'catch-up ', 1001, 55000)
  items = appendThinkingChunk(items, 'think.', 1002, 100000)
  assert.equal(items.length, 1)
  assert.equal(items[0].lastAt, 1002, 'lastAt tracks the most recent delta')
  assert.equal(items[0].duration_ms, 90000,
    'frozen label uses runner span, not bursty client replay delta')
})

test('thinkingElapsedMs anchors live elapsed to runner time across a replay', () => {
  // LIVE: block opens at client 1000 / runner 10000, latest delta at client
  // 4000 / runner 13000 → 3s runner span. now=5000 (1s after) → 3s + 1s tail.
  let live = []
  live = appendThinkingChunk(live, 'a', 1000, 10000)
  live = appendThinkingChunk(live, 'b', 4000, 13000)
  assert.equal(thinkingElapsedMs(live[0], 5000), 4000)

  // RECONNECT: the SAME block replays as a burst — client clock jumps to ~90000
  // but the replayed events carry their ORIGINAL runner ts (10000, 13000).
  // Elapsed must stay runner-anchored (3s span + 0.5s tail), NOT restart at ~0.
  let replay = []
  replay = appendThinkingChunk(replay, 'a', 90000, 10000)
  replay = appendThinkingChunk(replay, 'b', 90000, 13000)
  assert.equal(thinkingElapsedMs(replay[0], 90500), 3500)
  // The old formula (now - startedAt) would give 90500 - 90000 = 500ms → "1s".
  assert.ok(thinkingElapsedMs(replay[0], 90500) > 3000,
    'replayed block reports its true elapsed, not a reset-to-1s')
})

test('thinkingElapsedMs falls back to startedAt for legacy items without lastAt', () => {
  const legacy = { type: 'thinking', content: 'x', startedAt: 1000 }
  assert.equal(thinkingElapsedMs(legacy, 4000), 3000)
})

test('catch-up completion restores quiet thinking time after a chat remount', () => {
  let replay = []
  replay = appendThinkingChunk(replay, 'Scheduling poll after delay', 90_000, 10_000)

  const anchored = anchorReplayedThinking(replay, 70_000, 150_000)
  assert.equal(anchored[0].duration_ms, 60_000,
    'server replay time includes the silent interval after the only delta')
  assert.equal(anchored[0].lastAt, 150_000)
  assert.equal(thinkingElapsedMs(anchored[0], 151_000), 61_000,
    'the live ticker continues from turn age instead of restarting at 1s')
})

test('catch-up anchoring only advances a trailing live thinking block', () => {
  const completed = [
    { type: 'thinking', content: 'plan', firstTs: 10_000, duration_ms: 2_000 },
    { type: 'tool', tool: 'Bash', status: 'running' },
  ]
  assert.equal(anchorReplayedThinking(completed, 70_000, 150_000), completed,
    'reasoning followed by visible work is already complete and stays frozen')
})

test('appendThinkingChunk falls back to client delta without runner timestamps', () => {
  let items = []
  items = appendThinkingChunk(items, 'Let me ', 1000)
  items = appendThinkingChunk(items, 'think.', 2500, null)
  assert.equal(items.length, 1)
  assert.equal(items[0].startedAt, 1000)
  assert.equal(items[0].firstTs, null)
  assert.equal(items[0].duration_ms, 1500)
})

test('appendThinkingChunk opens a fresh item after a non-thinking item', () => {
  // A thinking that arrives after text (or a tool) must NOT merge into the
  // text — it opens its own block so reasoning stays in emit-order.
  let items = [{ type: 'text', content: 'Here is the answer.' }]
  items = appendThinkingChunk(items, 'reconsidering...', 5000)
  assert.equal(items.length, 2)
  assert.equal(items[0].type, 'text')
  assert.deepEqual(items[1], {
    type: 'thinking',
    content: 'reconsidering...',
    startedAt: 5000,
    firstTs: null,
    lastAt: 5000,
    duration_ms: 0,
  })
})

test('appendThinkingChunk is a no-op on empty content', () => {
  const items = [{ type: 'thinking', content: 'x' }]
  assert.equal(appendThinkingChunk(items, ''), items, 'empty chunk returns the same array')
})

test('deferred thinking metadata clears the inline prefix and keeps identity', () => {
  let items = appendThinkingChunk(
    [], 'prefix', 1000, 10000, 'summary:0', { thinking_id: 'think-1' },
  )
  items = appendThinkingChunk(
    items, '', 1100, 10100, 'summary:0', {
      thinking_id: 'think-1',
      thinking_deferred: true,
      thinking_revision: 1200,
    },
  )
  assert.equal(items.length, 1)
  assert.equal(items[0].content, '')
  assert.equal(items[0].thinking_id, 'think-1')
  assert.equal(items[0].thinking_revision, 1200)
  assert.equal(items[0].thinking_deferred, true)
})

// ---------------------------------------------------------------------------
// reconcileStreamItems — the catch-up commit "return without redraw" merge
// (contract v2 item 2, lever 2c). The core assertion is OBJECT IDENTITY: an
// unchanged item keeps its reference across the commit, so a mid-stream return
// re-renders nothing. tool_use_id (lever 2a) is the identity key for tools.
// ---------------------------------------------------------------------------

test('reconcile preserves object identity for an unchanged tool item (the core assertion)', () => {
  const tool = { type: 'tool', tool: 'Bash', input: 'ls', output: 'a\nb', status: 'done', tool_use_id: 'toolu_1' }
  const prev = [{ type: 'text', content: 'hi' }, tool]
  // The catch-up replay rebuilds fresh objects carrying the SAME tool_use_id.
  const next = [{ type: 'text', content: 'hi' }, { ...tool }]

  const result = reconcileStreamItems(prev, next)

  assert.equal(result[1], tool, 'unchanged tool item keeps its exact object reference across the commit')
  assert.equal(result[0], prev[0], 'unchanged text item keeps its reference too')
  assert.equal(result, prev, 'a fully-unchanged replay returns the prev array itself (React bails out entirely)')
})

test('reconcile merges a tool that changed state, keeping its key so the DOM node survives', () => {
  const running = { type: 'tool', tool: 'Bash', input: 'ls', output: '', status: 'running', tool_use_id: 'toolu_1' }
  const prev = [running]
  // Same tool, now completed with output — the reconnect replays the finished form.
  const next = [{ type: 'tool', tool: 'Bash', input: 'ls', output: 'a\nb', status: 'done', tool_use_id: 'toolu_1' }]

  const result = reconcileStreamItems(prev, next)

  assert.notEqual(result[0], running, 'a changed tool becomes a new object (props must update)')
  assert.equal(result[0].tool_use_id, 'toolu_1', 'but the identity key is preserved, so React reuses the ToolBlock instance/DOM')
  assert.equal(result[0].status, 'done')
  assert.equal(result[0].output, 'a\nb')
})

test('reconcile never moves a live thinking clock backwards', () => {
  const prev = [{
    type: 'thinking', content: 'plan', startedAt: 1_000,
    firstTs: 10_000, lastAt: 20_000, duration_ms: 9_000,
  }]
  const replay = [{
    type: 'thinking', content: 'plan', startedAt: 30_000,
    firstTs: 15_000, lastAt: 30_000, duration_ms: 5_000,
  }]
  const result = reconcileStreamItems(prev, replay)
  assert.equal(result[0].duration_ms, 19_000,
    'the visible snapshot was already at 19s when replay committed')
  assert.equal(result[0].startedAt, 1_000)
})

test('reconcile never advances a completed non-trailing thinking clock', () => {
  const thought = {
    type: 'thinking', content: 'plan', startedAt: 1_000,
    firstTs: 10_000, lastAt: 20_000, duration_ms: 9_000,
  }
  const prev = [thought, { type: 'text', content: 'Done.' }]
  // Catch-up is assembled incrementally. At its first atomic commit it can
  // contain only this historical thought even though the visible snapshot
  // already knows prose follows it. That one-item replay tail is not live.
  const replay = [{
    type: 'thinking', content: 'plan', startedAt: 30_000,
    firstTs: 10_000, lastAt: 30_000, duration_ms: 9_000,
  }]

  const result = reconcileStreamItems(prev, replay)

  assert.equal(result[0].duration_ms, 9_000,
    'later replay wall time cannot be added after prose sealed the thought')
})

test('reconcile matches tools by tool_use_id, not position, and never merges two different tools', () => {
  const t1 = { type: 'tool', tool: 'Bash', input: 'ls', output: 'x', status: 'done', tool_use_id: 'toolu_1' }
  const prev = [t1]
  // The replay's first item is a DIFFERENT tool (different id) — a positional
  // collision must not silently inherit t1's identity.
  const t2 = { type: 'tool', tool: 'Grep', input: 'foo', output: 'y', status: 'done', tool_use_id: 'toolu_2' }
  const next = [t2]

  const result = reconcileStreamItems(prev, next)
  assert.notEqual(result[0], t1, 'different tool_use_id → the replayed object wins, not a merge onto t1')
  assert.equal(result[0].tool_use_id, 'toolu_2')
})

test('reconcile appends genuinely new replayed items without disturbing existing identity', () => {
  const tool = { type: 'tool', tool: 'Bash', input: 'ls', output: 'a', status: 'done', tool_use_id: 'toolu_1' }
  const prev = [tool]
  const next = [
    { ...tool },
    { type: 'tool', tool: 'Read', input: 'f', output: 'z', status: 'done', tool_use_id: 'toolu_2' },
    { type: 'text', content: 'summary' },
  ]

  const result = reconcileStreamItems(prev, next)
  assert.equal(result.length, 3)
  assert.equal(result[0], tool, 'existing tool keeps its reference')
  assert.equal(result[1].tool_use_id, 'toolu_2', 'new tool is appended')
  assert.equal(result[2].type, 'text')
})

test('reconcile drops on-screen items absent from the replay — never resurrects a steer-dropped segment', () => {
  const prev = [
    { type: 'text', content: 'pre-steer answer' },
    { type: 'tool', tool: 'Bash', input: 'ls', output: 'a', status: 'done', tool_use_id: 'toolu_1' },
  ]
  // Post-steer replay carries only the continuation — the pre-steer segment was
  // promoted to its own message and must not reappear.
  const next = [{ type: 'text', content: 'post-steer continuation' }]

  const result = reconcileStreamItems(prev, next)
  assert.equal(result.length, 1)
  assert.equal(result[0].content, 'post-steer continuation')
})

test('reconcile keeps the legacy ordinal (tokenless) path working positionally', () => {
  // Old chats / legacy wires carry no tool_use_id; both prev and next lack it,
  // so identity falls back to position — an unchanged legacy tool is still reused.
  const legacy = { type: 'tool', tool: 'Bash', input: 'ls', output: 'a', status: 'done' }
  const prev = [legacy]
  const result = reconcileStreamItems(prev, [{ ...legacy }])
  assert.equal(result, prev, 'legacy tokenless tool reuses identity positionally when unchanged')
})

test('reconcile onto a snapshot-seeded stream reconciles in place (no duplicate append)', () => {
  // Path A remount: streamSnapshotCache seeds the visible items (they carry
  // tool_use_id, so they survive JSON round-trip). The first catch-up commit
  // must reconcile onto them, not append a second copy.
  const seeded = JSON.parse(JSON.stringify([
    { type: 'text', content: 'partial' },
    { type: 'tool', tool: 'Bash', input: 'ls', output: '', status: 'running', tool_use_id: 'toolu_1' },
  ]))
  const replay = [
    { type: 'text', content: 'partial then more' },
    { type: 'tool', tool: 'Bash', input: 'ls', output: 'done', status: 'done', tool_use_id: 'toolu_1' },
  ]

  const result = reconcileStreamItems(seeded, replay)
  assert.equal(result.length, 2, 'no duplicate tool block appended — reconciled onto the seeded item by id')
  assert.equal(result[1].tool_use_id, 'toolu_1')
  assert.equal(result[1].status, 'done')
})

test('reconcile returns the fresh array when prev is empty', () => {
  const next = [{ type: 'text', content: 'x' }]
  assert.equal(reconcileStreamItems([], next), next)
})
