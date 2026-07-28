import assert from 'node:assert/strict'
import test from 'node:test'
import {
  activityStreamState,
  activityCollapsedLabel,
  thoughtDurationLabel,
  toolGroupPastSummary,
  activityDisplayState,
  activityMemoSig,
} from '../groupBlocks.js'
import { toolActivityIcon, toolActivityPastLabel } from '../toolActivityLabel.js'

// The label helpers are the single localization surface for the collapsed
// activity line — ActivityStretch owns only presentation and the 1Hz clock, so
// the exact copy the redesign settled on is pinned here on the pure functions.

const tool = (extra = {}) => ({ type: 'tool', ...extra })
const think = (extra = {}) => ({ type: 'thinking', ...extra })
const e = item => ({ item })

const failOutput = JSON.stringify({ stdout: '', stderr: 'boom', exit_code: 1 })

test('activityStreamState: a live thinking tail forces running (running-wins)', () => {
  // While the agent is actively reasoning the line reads in-progress, regardless
  // of the diagnostic result on an earlier command.
  assert.equal(activityStreamState([], { liveThinkingTail: true }), 'running')
  assert.equal(
    activityStreamState([tool({ status: 'done', output: failOutput })], { liveThinkingTail: true }),
    'running',
  )
})

test('activityStreamState: settled command failures stay inside expansion', () => {
  assert.equal(activityStreamState([]), 'done')
  assert.equal(activityStreamState([tool({ status: 'done', output: '{}' })]), 'done')
  assert.equal(activityStreamState([tool({ status: 'done', output: failOutput })]), 'done')
  assert.equal(activityStreamState([tool({ status: 'running' })]), 'running')
})

test('collapsed label — live tool running: the running-first activity rollup, no ellipsis', () => {
  const entries = [
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Bash', status: 'running' })),
  ]
  assert.equal(activityCollapsedLabel(entries, { live: true }), 'Running a command · Reading a file')
})

test('collapsed label — live thinking tail: a bare "Thinking" (no clock, no dots)', () => {
  // The shimmer is the only motion; the measured duration surfaces at settle.
  const entries = [e(think({ content: 'x', duration_ms: 5000, lastAt: 1_000_000 }))]
  assert.equal(activityCollapsedLabel(entries, { live: true }), 'Thinking')
})

test('collapsed label — settled thinking-only: "Thought for Ns", no ellipsis', () => {
  const entries = [e(think({ content: 'x', duration_ms: 12000 }))]
  assert.equal(activityCollapsedLabel(entries, { live: false }), 'Thought for 12 seconds')
})

test('collapsed label — settled thinking-only with no duration: bare "Thought"', () => {
  const entries = [e(think({ content: 'x' }))]
  assert.equal(activityCollapsedLabel(entries, { live: false }), 'Thought')
})

test('collapsed label — settled mixed stretch: past-tense sentence, tools only', () => {
  // The reasoning is available on expand; the settled line stays a short
  // scannable "what did it DO" summary in past tense — "Read a file, edited
  // code" (the Codex idiom), never a "Reading files" frozen in time.
  const entries = [
    e(think({ content: 'plan', duration_ms: 3000 })),
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Edit', status: 'done' })),
  ]
  assert.equal(activityCollapsedLabel(entries, { live: false }), 'Read a file, edited code')
})

test('toolGroupPastSummary: first-seen dedupe, lowercased continuations, raw names kept', () => {
  assert.equal(
    toolGroupPastSummary([
      tool({ tool: 'Bash' }), tool({ tool: 'Read' }), tool({ tool: 'Glob' }),
    ]),
    'Ran a command, read files',
  )
  // An unmapped tool is an identifier, not prose: casing survives mid-sentence.
  assert.equal(
    toolGroupPastSummary([tool({ tool: 'Read' }), tool({ tool: 'CronCreate' })]),
    'Read a file, CronCreate',
  )
  // Overflow folds into +N, same as the live rollup.
  assert.equal(
    toolGroupPastSummary([
      tool({ tool: 'Read' }), tool({ tool: 'Edit' }),
      tool({ tool: 'Bash' }), tool({ tool: 'Grep' }),
    ]),
    'Read a file, edited code, ran a command +1',
  )
})

test('collapsed label — LIVE mixed stretch keeps the progressive running-first rollup', () => {
  const entries = [
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Bash', status: 'running' })),
  ]
  assert.equal(activityCollapsedLabel(entries, { live: true }), 'Running a command · Reading a file')
})

test('a running tool keeps progressive copy outside the trailing live stretch', () => {
  const entries = [
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Bash', status: 'running' })),
  ]
  assert.equal(activityCollapsedLabel(entries, { live: false }), 'Running a command · Reading a file')
  assert.equal(activityStreamState(entries.map(entry => entry.item)), 'running')
})

test('settled activity labels and icons have neutral unknown-tool fallbacks', () => {
  assert.equal(toolActivityPastLabel('Bash'), 'Ran commands')
  assert.equal(toolActivityPastLabel('CronCreate'), null)
  assert.equal(toolActivityIcon('Bash'), 'terminal')
  assert.equal(toolActivityIcon('Grep'), 'search')
  assert.equal(toolActivityIcon('CronCreate'), 'dot')
  assert.equal(toolActivityIcon(undefined), 'dot')
})

test('collapsed label — a live thinking tail after a failed tool still reads "Thinking"', () => {
  const entries = [
    e(tool({ tool: 'Bash', status: 'done', output: failOutput })),
    e(think({ content: 'recovering', duration_ms: 1000, lastAt: 2_000_000 })),
  ]
  assert.equal(activityCollapsedLabel(entries, { live: true }), 'Thinking')
})

test('thoughtDurationLabel: whole seconds, clamps sub-second to 1s, bare "Thought" when unknown', () => {
  assert.equal(thoughtDurationLabel(12000), 'Thought for 12 seconds')
  assert.equal(thoughtDurationLabel(1), 'Thought for 1 second')
  assert.equal(thoughtDurationLabel(undefined), 'Thought')
})

test('activityDisplayState: a live stretch stays in-progress through the tool→tool gap', () => {
  // In the gap between one tool ending and the next event no tool is
  // 'running', but the trailing live stretch must keep its in-progress face —
  // spinner + progressive copy — never the settled glyph. Any legacy/internal
  // error state also projects to the calm settled overview once the turn ends.
  assert.equal(activityDisplayState('done', { live: true }), 'running')
  assert.equal(activityDisplayState('error', { live: true }), 'running')
  assert.equal(activityDisplayState('running', { live: true }), 'running')
  assert.equal(activityDisplayState('done', { live: false }), 'done')
  assert.equal(activityDisplayState('error', { live: false }), 'done')
})

test('activityMemoSig: command output and thinking text do not churn the overview', () => {
  const sigOf = output => activityMemoSig([e(tool({ tool: 'Bash', status: 'done', output }))])
  const okJson = '{"exit_code":0,"stdout":"abcdefghijklmnop"}'
  const failJson = '{"exit_code":1,"stdout":"abcdefghijklmnop"}'
  assert.equal(sigOf(okJson), sigOf(failJson))

  const base = [e(tool({ tool: 'Read', status: 'done' })), e(think({ content: 'a' }))]
  const grown = [e(tool({ tool: 'Read', status: 'done' })), e(think({ content: 'a much longer thought' }))]
  assert.equal(activityMemoSig(base), activityMemoSig(grown))
})
