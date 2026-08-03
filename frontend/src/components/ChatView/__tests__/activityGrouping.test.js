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
