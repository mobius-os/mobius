import test from 'node:test'
import assert from 'node:assert/strict'
import { groupTimelineRows } from '../timelineRowGrouping.js'

const helper = id => ({ id, type: 'helper_result' })
const peer = id => ({ id, type: 'peer_message' })

test('adjacent helper completions share one result group', () => {
  const helpers = [helper('one'), helper('two'), helper('three')]
  assert.deepEqual(groupTimelineRows(helpers), [helpers])
})

test('ordinary chat activity remains a helper-result grouping boundary', () => {
  const first = helper('one'), note = peer('note'), second = helper('two')
  assert.deepEqual(groupTimelineRows([first, note, second]), [
    [first], [note], [second],
  ])
})

test('adjacent peer messages share one exchange group', () => {
  const messages = [peer('one'), peer('two'), peer('three')]
  assert.deepEqual(groupTimelineRows(messages), [messages])
})
