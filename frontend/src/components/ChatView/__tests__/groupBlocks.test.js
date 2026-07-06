import assert from 'node:assert/strict'
import test from 'node:test'
import { groupToolRuns, toolGroupState, toolGroupSummary } from '../groupBlocks.js'

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
  // Running WINS over an already-failed sibling — the group must stay expanded
  // to show the live tool; the failure surfaces once the run settles.
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

test('toolGroupSummary: distinct names, first 3 + overflow', () => {
  assert.equal(
    toolGroupSummary([
      tool({ tool: 'Read' }), tool({ tool: 'Read' }), tool({ tool: 'Edit' }),
      tool({ tool: 'Bash' }), tool({ tool: 'Grep' }), tool({ tool: 'Write' }),
    ]),
    'Read, Edit, Bash +2'
  )
  assert.equal(
    toolGroupSummary([tool({ tool: 'Read' }), tool({ tool: 'Edit' })]),
    'Read, Edit'
  )
  assert.equal(toolGroupSummary([tool({}), tool({})]), 'Tool')
})
