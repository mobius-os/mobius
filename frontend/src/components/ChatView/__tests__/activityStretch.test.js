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

test('collapsed label — settled mixed stretch: past-tense sentence, tools only', () => {
  // The reasoning is available on expand; the settled line stays a short
  // scannable "what did it DO" summary in past tense — "Read files, edited
  // code" (the Codex idiom), never a "Reading files" frozen in time.
  const entries = [
    e(think({ content: 'plan', duration_ms: 3000 })),
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Edit', status: 'done' })),
  ]
  const label = activityCollapsedLabel(entries, { live: false })
  assert.equal(label.text, 'Read files, edited code')
  assert.equal(label.showEllipsis, false)
})

test('toolGroupPastSummary: first-seen dedupe, lowercased continuations, raw names kept', () => {
  assert.equal(
    toolGroupPastSummary([
      tool({ tool: 'Bash' }), tool({ tool: 'Read' }), tool({ tool: 'Glob' }),
    ]),
    'Ran commands, read files',
  )
  // An unmapped tool is an identifier, not prose: casing survives mid-sentence.
  assert.equal(
    toolGroupPastSummary([tool({ tool: 'Read' }), tool({ tool: 'CronCreate' })]),
    'Read files, CronCreate',
  )
  // Overflow folds into +N, same as the live rollup.
  assert.equal(
    toolGroupPastSummary([
      tool({ tool: 'Read' }), tool({ tool: 'Edit' }),
      tool({ tool: 'Bash' }), tool({ tool: 'Grep' }),
    ]),
    'Read files, edited code, ran commands +1',
  )
})

test('collapsed label — LIVE mixed stretch keeps the progressive running-first rollup', () => {
  const entries = [
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Bash', status: 'running' })),
  ]
  const label = activityCollapsedLabel(entries, { live: true })
  assert.equal(label.text, 'Running commands · Reading files')
})

test('a running tool keeps progressive copy outside the trailing live stretch', () => {
  const entries = [
    e(tool({ tool: 'Read', status: 'done' })),
    e(tool({ tool: 'Bash', status: 'running' })),
  ]
  const label = activityCollapsedLabel(entries, { live: false })
  assert.equal(label.text, 'Running commands · Reading files')
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

test('activityDisplayState: a live stretch stays in-progress through the tool→tool gap', () => {
  // In the gap between one tool ending and the next event no tool is
  // 'running', but the trailing live stretch must keep its in-progress face —
  // spinner + progressive copy — never the settled glyph, and never the
  // failure triangle beside present-tense text (failure waits for settle).
  assert.equal(activityDisplayState('done', { live: true }), 'running')
  assert.equal(activityDisplayState('error', { live: true }), 'running')
  assert.equal(activityDisplayState('running', { live: true }), 'running')
  assert.equal(activityDisplayState('done', { live: false }), 'done')
  assert.equal(activityDisplayState('error', { live: false }), 'error')
})

test('activityMemoSig: an equal-length output replacement that flips the exit code changes the sig', () => {
  // Claude's plain-text failure marker is START-anchored, so the head slice
  // must catch it; a JSON envelope can serialize exit_code first or last, so
  // head + tail together cover both. Same length everywhere by construction.
  const sigOf = output => activityMemoSig([e(tool({ tool: 'Bash', status: 'done', output }))])
  const okJson = '{"exit_code":0,"stdout":"abcdefghijklmnop"}'
  const failJson = '{"exit_code":1,"stdout":"abcdefghijklmnop"}'
  assert.equal(okJson.length, failJson.length)
  assert.notEqual(sigOf(okJson), sigOf(failJson))

  const okText = 'all good here padded to length!!'
  const failText = 'Exit code 1\nboom padded to len!!'
  assert.equal(okText.length, failText.length)
  assert.notEqual(sigOf(okText), sigOf(failText))

  // Thinking content stays OUT of the sig: a typewriter delta on the tail
  // thinking entry must not bust the memo.
  const base = [e(tool({ tool: 'Read', status: 'done' })), e(think({ content: 'a' }))]
  const grown = [e(tool({ tool: 'Read', status: 'done' })), e(think({ content: 'a much longer thought' }))]
  assert.equal(activityMemoSig(base), activityMemoSig(grown))
})
