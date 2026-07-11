import assert from 'node:assert/strict'
import test from 'node:test'
import {
  groupToolRuns,
  coalesceThinkingEntries,
  toolGroupState,
  toolGroupSummary,
} from '../groupBlocks.js'
import { toolActivityLabel } from '../toolActivityLabel.js'

const e = item => ({ item })
const tool = (extra = {}) => ({ type: 'tool', ...extra })

test('a lone tool renders as an activity group', () => {
  const nodes = groupToolRuns([e(tool()), e({ type: 'text' })])
  assert.equal(nodes.length, 2)
  assert.equal(nodes[0].group.length, 1)
  assert.ok(nodes[1].single)
})

test('two adjacent tools group', () => {
  const nodes = groupToolRuns([e(tool()), e(tool())])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].group.length, 2)
})

test('non-tool entries break a run and pass through in order', () => {
  const items = [
    e(tool({ tool: 'Read' })),
    e(tool({ tool: 'Edit' })),
    e({ type: 'text', content: 'hi' }),
    e(tool({ tool: 'Bash' })),
    e(tool({ tool: 'Bash' })),
  ]
  const nodes = groupToolRuns(items)
  // group(Read,Edit) · single(text) · group(Bash,Bash)
  assert.equal(nodes.length, 3)
  assert.equal(nodes[0].group.length, 2)
  assert.equal(nodes[1].single.item.type, 'text')
  assert.equal(nodes[2].group.length, 2)
})

test('original entry metadata is carried through untouched', () => {
  const items = [{ item: tool(), idx: 7 }, { item: tool(), idx: 8 }]
  const nodes = groupToolRuns(items)
  assert.deepEqual(nodes[0].group.map(x => x.idx), [7, 8])
})

test('a single tool between two texts keeps order as a one-tool group', () => {
  const nodes = groupToolRuns([e({ type: 'text' }), e(tool()), e({ type: 'text' })])
  assert.equal(nodes.length, 3)
  assert.ok(nodes[0].single)
  assert.equal(nodes[1].group.length, 1)
  assert.ok(nodes[2].single)
})

test('empty input yields no nodes', () => {
  assert.deepEqual(groupToolRuns([]), [])
})

// A failed shell result — the ONLY failure signal a tool block carries, since
// the stream never sets a tool status beyond running→done.
const failOutput = JSON.stringify({ stdout: '', stderr: 'boom', exit_code: 1 })
const okOutput = JSON.stringify({ stdout: 'ok', exit_code: 0 })

test('toolGroupState: running wins, then a nonzero exit is error, else done', () => {
  // A finished tool with a nonzero exit code marks the whole group failed,
  // even though its status is the usual 'done'.
  assert.equal(
    toolGroupState([tool({ status: 'done', output: okOutput }), tool({ status: 'done', output: failOutput })]),
    'error'
  )
  // A still-running tool has no final output yet, so it can't be "failed".
  assert.equal(
    toolGroupState([tool({ status: 'done', output: okOutput }), tool({ status: 'running' })]),
    'running'
  )
  // Running WINS over an already-failed sibling — while live the header reads
  // in-progress (spinner); the failure surfaces once the run settles to 'error'.
  assert.equal(
    toolGroupState([tool({ status: 'done', output: failOutput }), tool({ status: 'running' })]),
    'running'
  )
  assert.equal(
    toolGroupState([tool({ status: 'done', output: okOutput }), tool({ status: 'done', output: okOutput })]),
    'done'
  )
  // A running tool whose (partial) output isn't a failed terminal stays running.
  assert.equal(toolGroupState([tool({ status: 'running', output: '' })]), 'running')
})

test('toolGroupSummary: distinct activity labels, first 3 + overflow', () => {
  // Write maps to the same activity as Edit ("Editing code"), so 6 tools /
  // 5 distinct names fold to 4 distinct activities → 3 shown + 1 overflow.
  assert.equal(
    toolGroupSummary([
      tool({ tool: 'Read' }), tool({ tool: 'Read' }), tool({ tool: 'Edit' }),
      tool({ tool: 'Bash' }), tool({ tool: 'Grep' }), tool({ tool: 'Write' }),
    ]),
    'Reading files · Editing code · Running commands +1'
  )
  assert.equal(
    toolGroupSummary([tool({ tool: 'Read' }), tool({ tool: 'Edit' })]),
    'Reading files · Editing code'
  )
  assert.equal(toolGroupSummary([tool({}), tool({})]), 'Tool')
})

test('toolGroupSummary: an unknown tool name passes through raw', () => {
  assert.equal(
    toolGroupSummary([tool({ tool: 'FooTool' }), tool({ tool: 'Bash' })]),
    'FooTool · Running commands'
  )
})

test('toolGroupSummary: the running tool leads while the run is live', () => {
  // The running tool is normally the tail; its activity reads FIRST so the
  // collapsed header says what is executing NOW, not the run's first tool.
  assert.equal(
    toolGroupSummary([
      tool({ tool: 'Read', status: 'done' }),
      tool({ tool: 'Edit', status: 'done' }),
      tool({ tool: 'Bash', status: 'running' }),
    ]),
    'Running commands · Reading files · Editing code'
  )
  // Dedupe still folds across the reorder: a running tool whose activity also
  // appears earlier collapses to the single leading entry (Glob → Reading files
  // === Read), so the label isn't shown twice.
  assert.equal(
    toolGroupSummary([
      tool({ tool: 'Read', status: 'done' }),
      tool({ tool: 'Grep', status: 'done' }),
      tool({ tool: 'Glob', status: 'running' }),
    ]),
    'Reading files · Searching the code'
  )
  // Leading the running tool still respects the first-3 + "+N" overflow shape.
  assert.equal(
    toolGroupSummary([
      tool({ tool: 'Read', status: 'done' }),
      tool({ tool: 'Grep', status: 'done' }),
      tool({ tool: 'Edit', status: 'done' }),
      tool({ tool: 'WebSearch', status: 'running' }),
    ]),
    'Browsing the web · Reading files · Searching the code +1'
  )
  // No running tool → plain first-seen order (the done/persisted header),
  // identical to the pre-change behavior.
  assert.equal(
    toolGroupSummary([
      tool({ tool: 'Bash', status: 'done' }),
      tool({ tool: 'Read', status: 'done' }),
    ]),
    'Running commands · Reading files'
  )
})

test('toolActivityLabel: maps known tools, falls back to the raw name', () => {
  assert.equal(toolActivityLabel('Glob'), 'Reading files')
  assert.equal(toolActivityLabel('WebSearch'), 'Browsing the web')
  assert.equal(toolActivityLabel('FooTool'), 'FooTool')
  assert.equal(toolActivityLabel(undefined), 'Tool')
  // Prototype-chain names must not resolve to Object internals.
  assert.equal(toolActivityLabel('constructor'), 'constructor')
})


const think = (content, duration_ms = 0) => ({ type: 'thinking', content, duration_ms })

test('coalesceThinkingEntries: adjacent thinking merges into one, content + duration summed', () => {
  const entries = [
    { item: think('The sl', 700), idx: 0 },
    { item: think('iders move', 500), idx: 1 },
  ]
  const out = coalesceThinkingEntries(entries)
  assert.equal(out.length, 1)
  assert.equal(out[0].item.content, 'The sliders move')
  assert.equal(out[0].item.duration_ms, 1200)
})

test('coalesceThinkingEntries: preserves the FIRST survivor idx so tool blockIdx stays absolute', () => {
  // Persisted shape [thinking, thinking, thinking, tool] — the fragmented case.
  // The tool MUST keep idx 3 so ToolBlock's GET /tool-output?i=3 hits the real block.
  const entries = [
    { item: think('a', 100), idx: 0 },
    { item: think('b', 100), idx: 1 },
    { item: think('c', 100), idx: 2 },
    { item: { type: 'tool', tool: 'Bash' }, idx: 3 },
  ]
  const out = coalesceThinkingEntries(entries)
  assert.equal(out.length, 2)
  assert.equal(out[0].idx, 0)
  assert.equal(out[0].item.content, 'abc')
  assert.equal(out[1].idx, 3)
  assert.equal(out[1].item.type, 'tool')
})

test('coalesceThinkingEntries: thinking separated by a tool stays two distinct blocks', () => {
  const entries = [
    { item: think('first', 100), idx: 0 },
    { item: { type: 'tool', tool: 'Bash' }, idx: 1 },
    { item: think('second', 100), idx: 2 },
  ]
  const out = coalesceThinkingEntries(entries)
  assert.deepEqual(out.map(x => x.item.type), ['thinking', 'tool', 'thinking'])
  assert.deepEqual(out.map(x => x.idx), [0, 1, 2])
})

test('coalesceThinkingEntries: groupToolRuns after coalesce yields one thinking single + tool group', () => {
  const entries = [
    { item: think('a', 100), idx: 0 },
    { item: think('b', 100), idx: 1 },
    { item: { type: 'tool', tool: 'Read' }, idx: 2 },
    { item: { type: 'tool', tool: 'Edit' }, idx: 3 },
  ]
  const nodes = groupToolRuns(coalesceThinkingEntries(entries))
  assert.equal(nodes.length, 2)
  assert.equal(nodes[0].single.item.type, 'thinking')
  assert.equal(nodes[0].single.item.content, 'ab')
  assert.equal(nodes[1].group.map(x => x.idx).join(','), '2,3')
})
