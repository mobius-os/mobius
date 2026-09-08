/**
 * A Memory lookup run as a background Bash task: the tool_output is the host's
 * placeholder (recall stays `searching` with the task id) and the recall is
 * settled by the task_done that the backend stamps it on. The host is a Bash
 * block — not a Task/Agent tool — so applyTaskEvent must still land it.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { applyTaskEvent } from '../streamReducers.js'

const bashTool = () => ({
  type: 'tool', tool: 'Bash', tool_use_id: 'tu-1', status: 'done',
  recall: { status: 'searching', task_id: 'bb6q', query: 'q' },
})

test('a task_done carrying a recall settles the deferring Bash block', () => {
  const settled = { status: 'hit', query: 'q', notes: [{ id: 'a', path: 'notes/a.md', title: 'A' }] }
  const items = applyTaskEvent([bashTool()], {
    type: 'task_done', task_id: 'bb6q', tool_use_id: 'tu-1', status: 'completed', recall: settled,
  }, 1000)
  assert.equal(items[0].recall, settled)
  // A Bash host never grows a helper chip; only its recall changes.
  assert.equal(items[0].subagent, undefined)
})

test('a task_done recall for an unknown task changes nothing', () => {
  const start = [bashTool()]
  const items = applyTaskEvent(start, {
    type: 'task_done', task_id: 'other', tool_use_id: 'tu-9', status: 'completed',
    recall: { status: 'hit', notes: [] },
  }, 1000)
  assert.equal(items, start)
})

test('a task_done without a recall leaves the deferring block untouched', () => {
  const start = [bashTool()]
  const items = applyTaskEvent(start, {
    type: 'task_done', task_id: 'bb6q', tool_use_id: 'tu-1', status: 'completed',
  }, 1000)
  assert.equal(items, start)
})
