import test from 'node:test'
import assert from 'node:assert/strict'
import { foldAppActivityOperations, groupActivityRuns } from '../activityGrouping.js'

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
