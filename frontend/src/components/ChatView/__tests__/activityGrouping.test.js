import test from 'node:test'
import assert from 'node:assert/strict'
import {
  foldAppActivityOperations,
  groupActivityRuns,
  joinRoutineToActivity,
} from '../activityGrouping.js'
import { activityCollapsedLabel } from '../groupBlocks.js'
import { mergeAdjacentCompactActivityEntries } from '../activityPosition.js'

const entry = (type, extra = {}) => ({ item: { type, ...extra } })

test('activity grouping preserves interleave and isolates distinctive tools', () => {
  const thought = entry('thinking')
  const command = entry('tool', { name: 'exec_command' })
  const prose = entry('text')
  const image = entry('tool', { tool: 'Read', input: '/tmp/preview.png' })
  const tail = entry('tool', { tool: 'Read', input: '/tmp/source.js' })

  assert.deepEqual(groupActivityRuns([
    thought, command, prose, image, tail,
  ]), [
    { group: [thought, command] },
    { single: prose },
    { group: [image] },
    { group: [tail] },
  ])
})

test('activity grouping handles empty and non-activity-only input', () => {
  const question = entry('question')
  assert.deepEqual(groupActivityRuns([]), [])
  assert.deepEqual(groupActivityRuns([question]), [{ single: question }])
})

test('transparent provider separators do not fragment one activity stretch', () => {
  const thought = entry('thinking', { content: 'Checking the files' })
  const command = entry('tool', { name: 'exec_command' })

  for (const content of ['', '\n  ']) {
    const separator = entry('text', { content })
    assert.deepEqual(groupActivityRuns([thought, separator, command]), [
      { group: [thought, command] },
    ])
  }
})

test('context compaction breaks activity stretches instead of joining tools', () => {
  const before = entry('tool', { tool: 'Read' })
  const compaction = entry('context_compaction', { provider: 'codex' })
  const after = entry('thinking')

  assert.deepEqual(groupActivityRuns([before, compaction, after]), [
    { group: [before] },
    { single: compaction },
    { group: [after] },
  ])
})

test('positioned helper completions stay inside the surrounding activity run', () => {
  const command = entry('tool', { tool: 'Bash' })
  const firstHelper = entry('helper_result', { id: 'first' })
  const thought = entry('thinking')
  const secondHelper = entry('helper_result', { id: 'second' })
  const edit = entry('tool', { tool: 'Edit' })
  const prose = entry('text', { content: 'Finished.' })

  assert.deepEqual(groupActivityRuns([
    command, firstHelper, thought, secondHelper, edit, prose,
  ]), [
    { group: [command, firstHelper, thought, secondHelper, edit] },
    { single: prose },
  ])
})

test('pages of one app operation render as one row listing every page', () => {
  const page = (status, label, labels, extra = {}) => entry('tool', {
    tool: 'Bash', status,
    app_activity: {
      app_slug: 'memory', status: 'succeeded', label,
      operation_key: 'lk:read:ab12',
      resources: labels.map(name => ({ label: name, intent: `note:${name}` })),
      ...extra,
    },
  })
  const first = page('done', 'Read a Memory page', ['A', 'B'])
  const prose = entry('text', { content: 'Reading on.' })
  const last = page('done', 'Finished reading 3 notes from Memory', ['B', 'C'])
  const otherApp = page('done', 'Other', ['Z'], { app_slug: 'notes' })

  const nodes = groupActivityRuns(foldAppActivityOperations([first, prose, last, otherApp]))
  const row = nodes[0].group[0]
  // The operation keeps its first slot and shows the finished wording with
  // every note read, once each; another app's identical key stays separate.
  assert.equal(nodes.length, 3)
  assert.equal(row.item.app_activity.label, 'Finished reading 3 notes from Memory')
  assert.deepEqual(row.item.app_activity.resources.map(r => r.label), ['A', 'B', 'C'])
  assert.equal(nodes[0].group.length, 1)
  assert.equal(nodes[1].single, prose)
  assert.equal(nodes[2].group[0], otherApp)

  // Grouping alone keeps every stored page, so cold-transcript preparation
  // (which shares it) never drops or copies a block.
  assert.deepEqual(groupActivityRuns([first, prose, last]), [
    { group: [first] },
    { single: prose },
    { group: [last] },
  ])
})

const routineSave = (extra = {}) => entry('tool', {
  tool: 'mcp__mobius_control__checkpoint_chat', status: 'done', routine: true, ...extra,
})

test('a settled routine save joins the reply\'s earlier activity line', () => {
  const command = { ...entry('tool', { tool: 'Bash', status: 'done' }), idx: 0 }
  const answer = { ...entry('text', { content: 'Done.' }), idx: 1 }
  const save = { ...routineSave(), idx: 2 }
  const capture = {
    ...entry('tool', {
      tool: 'mcp__mobius_control__notes_remember', status: 'done', routine: true,
      app_activity: { status: 'succeeded', app_slug: 'notes', label: 'Saved' },
    }),
    idx: 3,
  }

  const nodes = groupActivityRuns(joinRoutineToActivity([command, answer, save, capture]))
  assert.deepEqual(nodes, [{ group: [command, save, capture] }, { single: answer }])
  assert.equal(activityCollapsedLabel(nodes[0].group), 'Ran a command')
})

test('a saved reply\'s compact routine block joins its earlier compact line', () => {
  const work = entry('activity', { entries: [entry('tool', { tool: 'Bash', status: 'done' })], tool_count: 1 })
  const answer = entry('text', { content: 'Done.' })
  const saved = entry('activity', { entries: [routineSave()], tool_count: 1 })
  const merged = mergeAdjacentCompactActivityEntries(joinRoutineToActivity([work, answer, saved]))
  assert.equal(merged.length, 2)
  assert.equal(merged[0].item.type, 'activity')
  assert.equal(merged[1], answer)
  // A compact block that counts steps it does not show is not known routine.
  const partial = entry('activity', { entries: [routineSave()], tool_count: 2 })
  assert.deepEqual(joinRoutineToActivity([work, answer, partial]), [work, answer, partial])
})

test('failed or undeclared saves keep their own place and wording', () => {
  const command = entry('tool', { tool: 'Bash', status: 'done' })
  const answer = entry('text', { content: 'Done.' })
  for (const save of [
    routineSave({ output_exit_code: 1 }),
    routineSave({ status: 'failed' }),
    routineSave({ routine: undefined }),
    routineSave({ app_activity: { status: 'succeeded', warning: 'Saved partially' } }),
    routineSave({ app_activity: { status: 'failed', label: 'Could not save' } }),
    routineSave({ app_activity: { status: 'succeeded', receipt_missing: true } }),
  ]) {
    assert.deepEqual(joinRoutineToActivity([command, answer, save]), [command, answer, save])
  }
  // With no earlier activity the save stays put and still says what it did.
  const save = routineSave()
  assert.deepEqual(joinRoutineToActivity([answer, save]), [answer, save])
  assert.equal(activityCollapsedLabel([save]), 'Saved notes')
})

test('a routine save never names reasoning or a distinctive line', () => {
  const thought = entry('thinking', { duration_ms: 3000 })
  const capture = entry('tool', {
    tool: 'mcp__mobius_control__notes_remember', status: 'done', routine: true,
    app_activity: { status: 'succeeded', app_slug: 'notes', label: 'Saved' },
  })
  assert.deepEqual(groupActivityRuns([thought, capture]), [{ group: [thought, capture] }])
  assert.equal(activityCollapsedLabel([thought, capture]), 'Thought for 3 seconds')
  // A save still running beside work does not take over the live line either.
  const command = entry('tool', { tool: 'Bash', status: 'done' })
  const saving = routineSave({ status: 'running' })
  assert.deepEqual(groupActivityRuns([command, saving]), [{ group: [command, saving] }])
  assert.equal(activityCollapsedLabel([command, saving], { live: true }), 'Running a command')
  assert.equal(activityCollapsedLabel([saving], { live: true }), 'Saving notes')
})
