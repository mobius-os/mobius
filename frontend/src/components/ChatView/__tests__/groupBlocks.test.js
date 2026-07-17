import assert from 'node:assert/strict'
import test from 'node:test'
import {
  groupActivityRuns,
  coalesceThinkingEntries,
  toolGroupState,
  toolGroupSummary,
} from '../groupBlocks.js'
import {
  toolActivityLabel,
  effectiveToolName,
  isDistinctiveActivityTool,
} from '../toolActivityLabel.js'

const e = item => ({ item })
const tool = (extra = {}) => ({ type: 'tool', ...extra })

test('a lone tool renders as an activity group', () => {
  const nodes = groupActivityRuns([e(tool()), e({ type: 'text' })])
  assert.equal(nodes.length, 2)
  assert.equal(nodes[0].group.length, 1)
  assert.ok(nodes[1].single)
})

test('a lone thinking renders as an activity group', () => {
  // Thinking is now an activity entry too, so a standalone thinking block folds
  // into the same primitive (a 1-entry group) rather than the old standalone
  // "> Thought" disclosure.
  const nodes = groupActivityRuns([e({ type: 'thinking' }), e({ type: 'text' })])
  assert.equal(nodes.length, 2)
  assert.equal(nodes[0].group.length, 1)
  assert.equal(nodes[0].group[0].item.type, 'thinking')
  assert.ok(nodes[1].single)
})

test('two adjacent tools group', () => {
  const nodes = groupActivityRuns([e(tool()), e(tool())])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].group.length, 2)
})

test('thinking and tools unify into one stretch in emit order', () => {
  // thinking → Read → thinking → Bash is ONE activity stretch (nothing prose-like
  // between the pieces), the single biggest real-estate win of the redesign.
  const items = [
    e({ type: 'thinking', content: 'plan' }),
    e(tool({ tool: 'Read' })),
    e({ type: 'thinking', content: 'more' }),
    e(tool({ tool: 'Bash' })),
  ]
  const nodes = groupActivityRuns(items)
  assert.equal(nodes.length, 1)
  assert.deepEqual(nodes[0].group.map(x => x.item.type), ['thinking', 'tool', 'thinking', 'tool'])
})

test('text, question, and error each break a stretch and pass through in order', () => {
  const items = [
    e({ type: 'thinking' }),
    e(tool({ tool: 'Read' })),
    e({ type: 'text', content: 'hi' }),
    e(tool({ tool: 'Bash' })),
    e({ type: 'question' }),
    e(tool({ tool: 'Edit' })),
    e({ type: 'error' }),
  ]
  const nodes = groupActivityRuns(items)
  // group(think,Read) · text · group(Bash) · question · group(Edit) · error
  assert.deepEqual(
    nodes.map(n => (n.group ? `group:${n.group.length}` : `single:${n.single.item.type}`)),
    ['group:2', 'single:text', 'group:1', 'single:question', 'group:1', 'single:error'],
  )
})

test('original entry metadata is carried through untouched', () => {
  const items = [{ item: tool(), idx: 7 }, { item: tool(), idx: 8 }]
  const nodes = groupActivityRuns(items)
  assert.deepEqual(nodes[0].group.map(x => x.idx), [7, 8])
})

test('a thinking-first stretch keeps the thinking entry first for keying', () => {
  // The stretch is keyed by its FIRST entry, so a thinking-first stretch that
  // later gains a tool must keep the thinking entry (its idx) at position 0.
  const items = [
    { item: { type: 'thinking' }, idx: 2 },
    { item: tool({ tool: 'Read', tool_use_id: 'x' }), idx: 3 },
  ]
  const nodes = groupActivityRuns(items)
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].group[0].item.type, 'thinking')
  assert.equal(nodes[0].group[0].idx, 2)
})

test('empty input yields no nodes', () => {
  assert.deepEqual(groupActivityRuns([]), [])
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
    // Read x2 stays plural; the lone Bash reads singular.
    'Reading files · Editing code · Running a command +1'
  )
  assert.equal(
    toolGroupSummary([tool({ tool: 'Read' }), tool({ tool: 'Edit' })]),
    'Reading a file · Editing code'
  )
  assert.equal(toolGroupSummary([tool({}), tool({})]), 'Tool')
})

test('toolGroupSummary: an unknown tool name passes through raw', () => {
  assert.equal(
    toolGroupSummary([tool({ tool: 'FooTool' }), tool({ tool: 'Bash' })]),
    'FooTool · Running a command'
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
    'Running a command · Reading a file · Editing code'
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
    'Browsing the web · Reading a file · Searching the code +1'
  )
  // No running tool → plain first-seen order (the done/persisted header),
  // identical to the pre-change behavior.
  assert.equal(
    toolGroupSummary([
      tool({ tool: 'Bash', status: 'done' }),
      tool({ tool: 'Read', status: 'done' }),
    ]),
    'Running a command · Reading a file'
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

test('coalesceThinkingEntries: preserves the FIRST survivor idx so a tokenless tool keeps a stable key', () => {
  // Persisted shape [thinking, thinking, thinking, tool] — the fragmented case.
  // The tool MUST keep idx 3 so a tokenless tool's `t-<idx>` React key is stable.
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

test('coalesceThinkingEntries: groupActivityRuns after coalesce yields one unified stretch', () => {
  // Adjacent thinking coalesces first (preserving the first idx), then the
  // widened predicate folds the coalesced thinking + the two tools into ONE
  // stretch keyed by the thinking's idx 0.
  const entries = [
    { item: think('a', 100), idx: 0 },
    { item: think('b', 100), idx: 1 },
    { item: { type: 'tool', tool: 'Read' }, idx: 2 },
    { item: { type: 'tool', tool: 'Edit' }, idx: 3 },
  ]
  const nodes = groupActivityRuns(coalesceThinkingEntries(entries))
  assert.equal(nodes.length, 1)
  assert.deepEqual(nodes[0].group.map(x => x.item.type), ['thinking', 'tool', 'tool'])
  assert.equal(nodes[0].group[0].item.content, 'ab')
  assert.deepEqual(nodes[0].group.map(x => x.idx), [0, 2, 3])
})

test('toolGroupState: reads output_exit_code field on a reduced block', () => {
  // Contract rule 6: a failed tool whose output is only a carved excerpt (that
  // may not re-parse) still resolves the group to 'error' via the explicit
  // output_exit_code field.
  const carved = { type: 'tool', status: 'done', output: 'head…[9 B]…tail', output_exit_code: 1 }
  const ok = { type: 'tool', status: 'done', output: 'fine', output_exit_code: 0 }
  assert.equal(toolGroupState([ok, carved]), 'error')
  assert.equal(toolGroupState([ok, ok]), 'done')
  // A still-running tool keeps the group 'running' even with a failed sibling.
  assert.equal(toolGroupState([carved, { type: 'tool', status: 'running' }]), 'running')
})

test('a distinctive tool (image view) breaks out into its own line', () => {
  // A Read of an image is a ViewImage activity — a notable beat that stands on
  // its own line instead of folding into the surrounding mundane run (owner ref
  // 2026-07-17). Around it, the read/edit/bash plumbing still folds normally.
  const img = '/tmp/shot.png'  // real wire shape: tool.input is the STRING path
  const nodes = groupActivityRuns([
    e(tool({ tool: 'Edit' })),
    e(tool({ tool: 'Bash' })),
    e(tool({ tool: 'Read', input: img })),          // image → own line
    e(tool({ tool: 'Read', input: '/x.js' })),  // plain read, own run
    e(tool({ tool: 'Read', input: img })),          // image → own line
  ])
  // [Edit,Bash] fold · image · [plain Read] · image
  assert.equal(nodes.length, 4)
  assert.equal(nodes[0].group.length, 2)            // Edit + Bash folded
  assert.equal(nodes[1].group.length, 1)            // the image view, alone
  assert.equal(nodes[1].group[0].item.input, img)
  assert.equal(nodes[2].group.length, 1)            // the plain read, its own run
  assert.equal(nodes[3].group.length, 1)            // the trailing image view, alone
})

test('consecutive image views each get their own line — they do not accumulate', () => {
  const img = '/a.png'
  const nodes = groupActivityRuns([
    e(tool({ tool: 'Read', input: img })),
    e(tool({ tool: 'Read', input: img })),
    e(tool({ tool: 'Read', input: img })),
  ])
  assert.equal(nodes.length, 3)
  for (const n of nodes) assert.equal(n.group.length, 1)
})

test('effectiveToolName classifies image reads from the STRING wire input', () => {
  // Production never sends tool.input as an object: summarize_tool_input turns a
  // Read into the bare file_path STRING (backend/app/tool_summaries.py), and the
  // useStreamConnection tool-item contract types input as a string. The earlier
  // object-shaped fixtures hid that the classifier only worked on a shape that
  // never occurs at runtime; pin the real shape here so it can't regress inert.
  const T = (tool, input) => ({ type: 'tool', tool, input })
  assert.equal(effectiveToolName(T('Read', '/tmp/shot.png')), 'ViewImage')
  assert.equal(effectiveToolName(T('Read', '/tmp/SHOT.PNG')), 'ViewImage')       // case-insensitive
  assert.equal(effectiveToolName(T('Read', '/tmp/a.jpeg?token=x')), 'ViewImage') // query suffix
  assert.equal(effectiveToolName(T('Read', '/src/app.js')), 'Read')              // non-image folds
  assert.equal(effectiveToolName(T('Bash', 'ls')), 'Bash')
  // Defensive: an object shape (unit fixtures / any future source) still classifies.
  assert.equal(effectiveToolName(T('Read', { file_path: '/o.webp' })), 'ViewImage')
  // isDistinctiveActivityTool follows the string classification.
  assert.equal(isDistinctiveActivityTool(T('Read', '/x.png')), true)
  assert.equal(isDistinctiveActivityTool(T('Read', '/x.js')), false)
})
