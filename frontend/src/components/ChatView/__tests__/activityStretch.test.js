import assert from 'node:assert/strict'
import test from 'node:test'
import {
  activityStreamState,
  activityCollapsedLabel,
  thoughtDurationLabel,
} from '../groupBlocks.js'

// The label helpers are the single localization surface for the collapsed
// activity line — ActivityStretch owns only presentation and the 1Hz clock, so
// the exact copy the redesign settled on is pinned here on the pure functions.

const tool = (extra = {}) => ({ type: 'tool', ...extra })
const think = (extra = {}) => ({ type: 'thinking', ...extra })
const e = item => ({ item })

// A failed shell result — the only failure signal a tool block carries.
const failOutput = JSON.stringify({ stdout: '', stderr: 'boom', exit_code: 1 })

test('activityStreamState: a live thinking tail forces running (running-wins)', () => {
  // While the agent is actively reasoning the line reads in-progress, even if an
  // earlier tool already failed — the failure surfaces at settle, not mid-run.
  assert.equal(activityStreamState([], { liveThinkingTail: true }), 'running')
  assert.equal(
    activityStreamState([tool({ status: 'done', output: failOutput })], { liveThinkingTail: true }),
    'running',
  )
})

test('activityStreamState: settles to done/error/running from the tools when not live-thinking', () => {
  assert.equal(activityStreamState([]), 'done')
  assert.equal(activityStreamState([tool({ status: 'done', output: '{}' })]), 'done')
  assert.equal(activityStreamState([tool({ status: 'done', output: failOutput })]), 'error')
  assert.equal(activityStreamState([tool({ status: 'running' })]), 'running')
})

test('collapsed label — live tool running: the running-first activity rollup, no ellipsis', () => {
  const entries = [
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Bash', status: 'running' })),
  ]
  const label = activityCollapsedLabel(entries, { live: true })
  assert.equal(label.text, 'Running commands · Reading files')
  assert.equal(label.showEllipsis, false)
})

test('collapsed label — live thinking tail: "Thinking for Ns" + ellipsis', () => {
  // duration_ms + lastAt anchor the elapsed; passing now === lastAt yields the
  // exact stored duration, so the copy is deterministic.
  const now = 1_000_000
  const entries = [e(think({ content: 'x', duration_ms: 5000, lastAt: now }))]
  const label = activityCollapsedLabel(entries, { live: true, now })
  assert.equal(label.text, 'Thinking for 5 seconds')
  assert.equal(label.showEllipsis, true)
})

test('collapsed label — settled thinking-only: "Thought for Ns", no ellipsis', () => {
  const entries = [e(think({ content: 'x', duration_ms: 12000 }))]
  const label = activityCollapsedLabel(entries, { live: false })
  assert.equal(label.text, 'Thought for 12 seconds')
  assert.equal(label.showEllipsis, false)
})

test('collapsed label — settled thinking-only with no duration: bare "Thought"', () => {
  const entries = [e(think({ content: 'x' }))]
  assert.equal(activityCollapsedLabel(entries, { live: false }).text, 'Thought')
})

test('collapsed label — settled mixed stretch shows tool activities only', () => {
  // The reasoning is available on expand; the settled line stays a short scannable
  // "what did it DO" summary rather than repeating the thinking.
  const entries = [
    e(think({ content: 'plan', duration_ms: 3000 })),
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Edit', status: 'done' })),
  ]
  const label = activityCollapsedLabel(entries, { live: false })
  assert.equal(label.text, 'Reading files · Editing code')
  assert.equal(label.showEllipsis, false)
})

test('collapsed label — a live thinking tail after a failed tool still reads "Thinking"', () => {
  // running-wins: the danger chip waits for settle; live, the line is the ticker.
  const now = 2_000_000
  const entries = [
    e(tool({ tool: 'Bash', status: 'done', output: failOutput })),
    e(think({ content: 'recovering', duration_ms: 1000, lastAt: now })),
  ]
  const label = activityCollapsedLabel(entries, { live: true, now })
  assert.equal(label.text, 'Thinking for 1 second')
  assert.equal(label.showEllipsis, true)
})

test('thoughtDurationLabel: whole seconds, clamps sub-second to 1s, bare "Thought" when unknown', () => {
  assert.equal(thoughtDurationLabel(12000), 'Thought for 12 seconds')
  assert.equal(thoughtDurationLabel(1), 'Thought for 1 second')
  assert.equal(thoughtDurationLabel(undefined), 'Thought')
})
