import test from 'node:test'
import assert from 'node:assert/strict'
import { groupActivityRuns } from '../activityGrouping.js'

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
