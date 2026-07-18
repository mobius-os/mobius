/**
 * Pure-function tests for applyTaskEvent (streamReducers.js) — the single
 * reducer that ENRICHES a Task/Agent tool block with background-helper metadata
 * from task_start / task_progress / task_done. There is no standalone stream
 * item: helpers live on `tool.subagent[task_id]`, the same shape backend card
 * 247 persists, so live/promoted/reloaded render identically.
 *
 * The load-bearing invariant is IDEMPOTENCY: a reconnect / catch-up burst
 * replays the whole lifecycle, and the reducer must leave exactly ONE helper on
 * the block. The double-replay test locks that in, alongside the ordering rules
 * (done-before-start, no terminal downgrade, task_id-first routing).
 *
 * Imports the REAL modules so the tests fail when the reducer drifts.
 *
 * Run with:
 *   cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/components/ChatView/__tests__/subagentChips.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { applyTaskEvent } from '../streamReducers.js'
import { streamItemToBlock } from '../streamPromotion.js'

function taskTool(tool_use_id = 'toolu_A', overrides = {}) {
  return {
    type: 'tool', tool: 'Task', input: '', output: '',
    status: 'running', tool_use_id, ...overrides,
  }
}
function startEvent(overrides = {}) {
  return {
    type: 'task_start',
    task_id: 't1',
    description: 'Research the pricing tiers',
    task_type: 'general-purpose',
    tool_use_id: 'toolu_A',
    ...overrides,
  }
}
function progressEvent(overrides = {}) {
  return {
    type: 'task_progress',
    task_id: 't1',
    usage: { input_tokens: 10 },
    last_tool_name: 'Grep',
    tool_use_id: 'toolu_A',
    ...overrides,
  }
}
function doneEvent(overrides = {}) {
  return {
    type: 'task_done',
    task_id: 't1',
    status: 'completed',
    summary: 'Found three tiers.',
    tool_use_id: 'toolu_A',
    ...overrides,
  }
}

function toolOf(items) {
  const tool = items.find(it => it.type === 'tool')
  assert.ok(tool, 'a Task tool block is present')
  return tool
}

// ---------------------------------------------------------------------------
// Enrichment: start → progress → done stamps onto the tool block
// ---------------------------------------------------------------------------

test('task_start enriches the matching Task tool block .subagent[task_id]', () => {
  const items = applyTaskEvent([taskTool()], startEvent(), 1000)
  const h = toolOf(items).subagent.t1
  assert.equal(h.description, 'Research the pricing tiers')
  assert.equal(h.task_type, 'general-purpose')
  assert.equal(h.status, 'running')
  assert.equal(h.startedAt, 1000)
})

test('task_progress refreshes last_tool_name + usage and keeps the description', () => {
  let items = applyTaskEvent([taskTool()], startEvent({ description: 'Research pricing' }), 1000)
  items = applyTaskEvent(items, progressEvent({ last_tool_name: 'Read' }), 2000)
  const h = toolOf(items).subagent.t1
  assert.equal(h.last_tool_name, 'Read')
  assert.deepEqual(h.usage, { input_tokens: 10 })
  assert.equal(h.description, 'Research pricing', 'progress must not wipe the description')
  assert.equal(h.status, 'running')
})

test('task_done flips terminal, normalizes completed→done, freezes summary', () => {
  let items = applyTaskEvent([taskTool()], startEvent(), 1000)
  items = applyTaskEvent(items, progressEvent(), 2000)
  items = applyTaskEvent(items, doneEvent(), 3000)
  const h = toolOf(items).subagent.t1
  assert.equal(h.status, 'done', 'completed maps to done')
  assert.equal(h.summary, 'Found three tiers.')
})

test('two task_ids on one tool_use_id become two helpers on the block', () => {
  let items = applyTaskEvent([taskTool()], startEvent({ task_id: 't1' }), 1000)
  items = applyTaskEvent(items, startEvent({ task_id: 't2', description: 'Draft copy' }), 1001)
  assert.deepEqual(Object.keys(toolOf(items).subagent).sort(), ['t1', 't2'])
})

// ---------------------------------------------------------------------------
// IDEMPOTENCY — the core reconnect-safety invariant
// ---------------------------------------------------------------------------

test('replaying the full task burst TWICE leaves ONE helper on the tool block', () => {
  const base = [taskTool()]
  const play = (items) => {
    let next = applyTaskEvent(items, startEvent(), 1000)
    next = applyTaskEvent(next, progressEvent(), 2000)
    next = applyTaskEvent(next, doneEvent(), 3000)
    return next
  }
  const items = play(play(base))
  const sub = toolOf(items).subagent
  assert.equal(Object.keys(sub).length, 1, 'no duplicate helper on replay')
  assert.equal(sub.t1.status, 'done', 'terminal state preserved across replay')
  assert.equal(sub.t1.summary, 'Found three tiers.')
})

test('a re-delivered task_start on a running helper is a same-ref no-op', () => {
  const started = applyTaskEvent([taskTool()], startEvent(), 1000)
  const again = applyTaskEvent(started, startEvent(), 1000)
  assert.equal(again, started)
})

test('a re-delivered task_done is a same-ref no-op', () => {
  let items = applyTaskEvent([taskTool()], startEvent(), 1000)
  items = applyTaskEvent(items, doneEvent(), 2000)
  const again = applyTaskEvent(items, doneEvent(), 2000)
  assert.equal(again, items)
})

// ---------------------------------------------------------------------------
// Ordering rules — no terminal downgrade, done-before-start, task_id routing
// ---------------------------------------------------------------------------

test('task_done materializes a helper even when task_start was missed', () => {
  const items = applyTaskEvent([taskTool()], doneEvent(), 3000)
  const h = toolOf(items).subagent.t1
  assert.equal(h.status, 'done')
  assert.equal(h.summary, 'Found three tiers.')
})

test('a late task_start does not downgrade a terminal helper', () => {
  let items = applyTaskEvent([taskTool()], doneEvent({ status: 'failed' }), 3000)
  items = applyTaskEvent(items, startEvent(), 9000)
  assert.equal(toolOf(items).subagent.t1.status, 'failed')
})

test('task_progress does not revive a terminal helper (same-ref no-op)', () => {
  let items = applyTaskEvent([taskTool()], startEvent(), 1000)
  items = applyTaskEvent(items, doneEvent(), 2000)
  const after = applyTaskEvent(items, progressEvent({ last_tool_name: 'Bash' }), 3000)
  assert.equal(after, items, 'progress on a terminal helper is a no-op')
  assert.equal(toolOf(after).subagent.t1.status, 'done')
})

test('a null-tool_use_id task_done routes to its helper by task_id', () => {
  // Claude's terminal TaskUpdatedMessage emits tool_use_id:null — it can only be
  // matched through the helper an earlier task_start already created.
  let items = applyTaskEvent([taskTool('toolu_A')], startEvent({ tool_use_id: 'toolu_A' }), 1000)
  items = applyTaskEvent(items, doneEvent({ tool_use_id: null, status: 'killed' }), 2000)
  assert.equal(toolOf(items).subagent.t1.status, 'killed')
})

// ---------------------------------------------------------------------------
// Status normalization + description/summary preservation
// ---------------------------------------------------------------------------

test('task_done normalizes completed→done, keeps failed/killed/stopped/done', () => {
  const cases = [
    ['completed', 'done'],
    ['done', 'done'],
    ['failed', 'failed'],
    ['killed', 'killed'],
    ['stopped', 'stopped'],
  ]
  for (const [wire, expected] of cases) {
    const items = applyTaskEvent([taskTool()], doneEvent({ status: wire }), 2000)
    assert.equal(toolOf(items).subagent.t1.status, expected, `${wire} → ${expected}`)
  }
})

test('a later empty-description task_start does not wipe an existing description', () => {
  let items = applyTaskEvent([taskTool()], startEvent({ description: 'Research pricing' }), 1000)
  items = applyTaskEvent(items, doneEvent({ summary: 'Done.' }), 2000)
  items = applyTaskEvent(items, startEvent({ description: '' }), 3000)
  const h = toolOf(items).subagent.t1
  assert.equal(h.description, 'Research pricing')
  assert.equal(h.summary, 'Done.')
})

// ---------------------------------------------------------------------------
// Routing edges
// ---------------------------------------------------------------------------

test('applyTaskEvent no-ops when there is no host Task tool block', () => {
  const base = [{ type: 'text', content: 'hi' }]
  assert.equal(applyTaskEvent(base, startEvent(), 1000), base)
})

test('applyTaskEvent no-ops on a malformed event (no task_id)', () => {
  const base = [taskTool()]
  assert.equal(applyTaskEvent(base, { type: 'task_start' }, 1000), base)
})

test('a task_start only matches a Task/Agent tool, not an unrelated tool', () => {
  const base = [{ type: 'tool', tool: 'Bash', status: 'running', tool_use_id: 'toolu_A' }]
  assert.equal(applyTaskEvent(base, startEvent(), 1000), base, 'Bash is not a delegating tool')
})

// ---------------------------------------------------------------------------
// The metadata rides promotion for free (streamItemToBlock spreads tool fields)
// ---------------------------------------------------------------------------

test('streamItemToBlock carries tool .subagent through promotion', () => {
  const tool = {
    type: 'tool', tool: 'Task', status: 'running', tool_use_id: 'toolu_A',
    subagent: { t1: { description: 'x', status: 'done', summary: 's' } },
  }
  const block = streamItemToBlock(tool, { finalize: true })
  assert.equal(block.type, 'tool')
  assert.deepEqual(block.subagent, { t1: { description: 'x', status: 'done', summary: 's' } })
  assert.equal(block.status, 'done', 'a running tool still seals done on finalize')
})
