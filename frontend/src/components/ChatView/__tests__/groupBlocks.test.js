import assert from 'node:assert/strict'
import test from 'node:test'
import { groupToolRuns, toolGroupState, toolGroupSummary } from '../groupBlocks.js'
import { toolActivityLabel } from '../toolActivityLabel.js'

const e = item => ({ item })
const tool = (extra = {}) => ({ type: 'tool', ...extra })

test('a lone tool stays ungrouped', () => {
  const nodes = groupToolRuns([e(tool()), e({ type: 'text' })])
  assert.equal(nodes.length, 2)
  assert.ok(nodes[0].single)
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

test('a single tool between two texts is not grouped', () => {
  const nodes = groupToolRuns([e({ type: 'text' }), e(tool()), e({ type: 'text' })])
  assert.equal(nodes.length, 3)
  assert.ok(nodes.every(n => n.single))
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
