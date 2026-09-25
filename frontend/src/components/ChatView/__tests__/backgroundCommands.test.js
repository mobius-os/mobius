/* A command is one row: a background command shows a live clock only while it
   runs, and its shell task never becomes a helper row. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
after(() => vite.close())
const { default: ToolBlock } = await vite.ssrLoadModule('/src/components/ChatView/ToolBlock.jsx')
const { default: ActivityStretch } = await vite.ssrLoadModule('/src/components/ChatView/ActivityStretch.jsx')
const { runningBackgroundTask, agentHelperEntries } = await vite.ssrLoadModule('/src/components/ChatView/toolTasks.js')
const { toolCallLabel } = await vite.ssrLoadModule('/src/components/ChatView/toolActivityLabel.js')

const cmd = 'npm test'
const bash = (status, task) => ({
  type: 'tool', tool: 'Bash', input: cmd, status, tool_use_id: 'toolu_1',
  ...(task ? { subagent: { b1a2b3c4d: { description: cmd, task_type: 'local_bash', ...task } } } : {}),
})
const backgrounded = () => bash('done', { status: 'running', startedAt: Date.now() - 65_000 })

test('a command still running after its call returned is a background command', () => {
  assert.ok(runningBackgroundTask(backgrounded()))
  assert.equal(runningBackgroundTask(bash('done', { status: 'done' })), null, 'finished')
  assert.equal(runningBackgroundTask(bash('running', { status: 'running' })), null, 'still foreground')
  assert.equal(agentHelperEntries(backgrounded()).length, 0, 'never a helper')
})

test('a background command row reads as running and shows only its clock', () => {
  assert.equal(toolCallLabel(backgrounded()), `Running ${cmd}`)
  const html = renderToStaticMarkup(React.createElement(ToolBlock, { t: backgrounded(), chatId: 'c' }))
  assert.match(html, /chat__tool-elapsed">1m 0[45]s</)
  assert.doesNotMatch(html, />background</)
  assert.match(html, /chat__tool-icon--running/)
})

test('a finished or foreground command carries no clock', () => {
  for (const t of [bash('done', { status: 'done', startedAt: 1 }), bash('running', { status: 'running', startedAt: 1 }), bash('done')]) {
    const html = renderToStaticMarkup(React.createElement(ToolBlock, { t, chatId: 'c' }))
    assert.doesNotMatch(html, /chat__tool-elapsed/)
  }
  assert.equal(toolCallLabel(bash('done', { status: 'done' })), `Ran ${cmd}`)
})

test('shell tasks add no helper row and no running count to a stretch', () => {
  const entries = [
    { idx: 0, item: backgrounded() },
    { idx: 1, item: { type: 'tool', tool: 'Read', input: 'a.js', status: 'done', tool_use_id: 'toolu_2' } },
  ]
  const html = renderToStaticMarkup(React.createElement(ActivityStretch, { entries, chatId: 'c', surfaceKey: 's' }))
  assert.doesNotMatch(html, /\d running/)
  assert.doesNotMatch(html, /chat__subagent\b/)
})

test('an agent helper still counts beside a shell task', () => {
  const task = {
    type: 'tool', tool: 'Agent', input: 'Review', status: 'running', tool_use_id: 'toolu_3',
    subagent: { a0123456789abcdef: { description: 'Review the diff', task_type: 'local_agent', status: 'running', startedAt: 1 } },
  }
  const html = renderToStaticMarkup(React.createElement(ActivityStretch, {
    entries: [{ idx: 0, item: backgrounded() }, { idx: 1, item: task }], chatId: 'c', surfaceKey: 's',
  }))
  assert.match(html, /1 running/)
})
