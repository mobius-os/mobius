/**
 * A Memory lookup run as a background Bash task: the tool_output is the host's
 * placeholder (recall stays `searching` with the task id) and the recall is
 * settled by the task_done that the backend stamps it on. The host is a Bash
 * block — not a Task/Agent tool — so applyTaskEvent must still land it.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { applyTaskEvent } from '../streamReducers.js'
import { agentHelperEntries } from '../toolTasks.js'

const bashTool = () => ({
  type: 'tool', tool: 'Bash', tool_use_id: 'tu-1', status: 'done',
  recall: { status: 'searching', task_id: 'bb6qa1b2c', query: 'q' },
})

const appActivityTool = () => ({
  type: 'tool', tool: 'Bash', tool_use_id: 'tu-app', status: 'done',
  app_activity: {
    status: 'running', task_id: 'task-app', app_slug: 'brain',
    app_name: 'Brain', activity_id: 'lookup', label: 'Searching',
  },
})

test('a task_done carrying a recall settles the deferring Bash block', () => {
  const settled = { status: 'hit', query: 'q', notes: [{ id: 'a', path: 'notes/a.md', title: 'A' }] }
  const items = applyTaskEvent([bashTool()], {
    type: 'task_done', task_id: 'bb6qa1b2c', tool_use_id: 'tu-1', status: 'completed', recall: settled,
  }, 1000)
  assert.equal(items[0].recall, settled)
  // The command records its own shell task, but a Bash host never grows a
  // helper row.
  assert.equal(agentHelperEntries(items[0]).length, 0)
})

test('a task_done recall for an unknown task changes nothing', () => {
  const start = [bashTool()]
  const items = applyTaskEvent(start, {
    type: 'task_done', task_id: 'other', tool_use_id: 'tu-9', status: 'completed',
    recall: { status: 'hit', notes: [] },
  }, 1000)
  assert.equal(items, start)
})

test('a task_done without a recall leaves the recall alone and adds no helper', () => {
  const start = [bashTool()]
  const items = applyTaskEvent(start, {
    type: 'task_done', task_id: 'bb6qa1b2c', tool_use_id: 'tu-1', status: 'completed',
  }, 1000)
  assert.equal(items[0].recall, start[0].recall)
  assert.equal(agentHelperEntries(items[0]).length, 0)
})

test('an app activity settles by task id when Claude omits the tool id', () => {
  const settled = {
    status: 'succeeded', app_slug: 'brain', app_name: 'Brain',
    activity_id: 'lookup', label: 'Found a note',
  }
  const items = applyTaskEvent([appActivityTool()], {
    type: 'task_done', task_id: 'task-app', tool_use_id: null,
    status: 'completed', app_activity: settled,
  }, 1000)
  assert.equal(items[0].app_activity, settled)
  assert.equal(items[0].subagent, undefined)
})
