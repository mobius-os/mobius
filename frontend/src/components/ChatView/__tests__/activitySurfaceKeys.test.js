import assert from 'node:assert/strict'
import test from 'node:test'
import { groupActivityRuns, coalesceThinkingEntries } from '../groupBlocks.js'
import { suppressedQuestionToolIndices } from '../streamReducers.js'
import { assistantBlockKey } from '../streamPromotion.js'

// The active answer has two surfaces that must reconcile without remounts: the
// live stream ABSORBS the AskUserQuestion tool twin into the question card,
// while the DB partial keeps both blocks and relies on render-time suppression.
// Raw msg.blocks ordinals therefore differ by one for everything after a
// mid-turn question — so entry idx is assigned from the POST-suppression
// position (see MsgContent). This test drives both surface shapes through the
// exact same pipeline MsgContent uses and asserts the stretch and entry keys
// come out identical: an ordinal-keyed thinking-first stretch after a question
// must NOT change key across the live↔DB switch, or React remounts it —
// collapsing what the user expanded and dropping nested lazy-fetch state.

// The same pipeline MsgContent runs before rendering: suppress the twin,
// number entries by their post-suppression position, coalesce legacy thinking
// fragments, group into activity stretches.
function pipeline(blocks) {
  const skip = suppressedQuestionToolIndices(blocks)
  const entries = blocks
    .map((block, i) => ({ item: block, rawIdx: i }))
    .filter(({ rawIdx }) => !skip.has(rawIdx))
    .map(({ item }, pos) => ({ item, idx: pos }))
  return groupActivityRuns(coalesceThinkingEntries(entries))
}

function stretchKeys(nodes) {
  return nodes
    .filter(n => n.group)
    .map(n => assistantBlockKey(n.group[0].item, n.group[0].idx))
}

const question = { type: 'question', questions: [{ question: 'Which?' }] }
const twin = { type: 'tool', tool: 'AskUserQuestion', input: '', output: '', status: 'done' }
const thinking = { type: 'thinking', content: 'plan the fix', duration_ms: 1200 }
// Tokenless (no tool_use_id), so its key is the ordinal fallback `t-<idx>` —
// the shape that exposes any cross-surface numbering drift.
const bash = { type: 'tool', tool: 'Bash', input: 'npm test', output: '', status: 'done' }

test('post-question stretch keys agree between the DB (twin kept) and live (twin absorbed) surfaces', () => {
  const dbBlocks = [twin, question, thinking, bash]
  const liveBlocks = [question, thinking, bash]

  const dbKeys = stretchKeys(pipeline(dbBlocks))
  const liveKeys = stretchKeys(pipeline(liveBlocks))

  assert.deepEqual(dbKeys, liveKeys,
    'the thinking-first stretch after the question must keep one key across the surface switch')
  // Both surfaces see: question(0) · stretch[thinking(1), bash(2)] — the
  // stretch keys on its first entry's position, past the suppressed twin.
  assert.deepEqual(dbKeys, [1])
})

test('a tokenless tool single after a question keeps its t-<idx> key across surfaces', () => {
  const dbBlocks = [twin, question, bash]
  const liveBlocks = [question, bash]

  assert.deepEqual(
    stretchKeys(pipeline(dbBlocks)),
    stretchKeys(pipeline(liveBlocks)),
  )
  assert.deepEqual(stretchKeys(pipeline(dbBlocks)), ['t-1'])
})

test('without a question card nothing is suppressed and numbering is the raw order', () => {
  const nodes = pipeline([thinking, bash, { type: 'text', content: 'done' }])
  assert.deepEqual(stretchKeys(nodes), [0])
  const single = nodes.find(n => n.single)
  assert.equal(single.single.idx, 2)
})

test('appending blocks mid-run never renumbers earlier entries', () => {
  const before = pipeline([twin, question, thinking])
  const after = pipeline([twin, question, thinking, bash, { type: 'text', content: 'hi' }])
  // The thinking-first stretch keeps key 1 as the run grows around it.
  assert.deepEqual(stretchKeys(before), [1])
  assert.equal(stretchKeys(after)[0], 1)
})
