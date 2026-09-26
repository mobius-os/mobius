/* Helper rows open the helper's own conversation; a working Möbius helper shows engine, step, and clock. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderWithModels } from './modelRegistryRender.js'
import { createServer } from 'vite'
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: SubagentChips } = await vite.ssrLoadModule('/src/components/ChatView/SubagentChips.jsx')
const { default: HelperResultCard, HelperResultGroupCard } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
const priorWindow = globalThis.window
globalThis.window = { location: new URL('https://mobius.test/shell') }
after(() => { globalThis.window = priorWindow; return vite.close() })

const chips = (subagent, props = {}) => renderWithModels(
  React.createElement(SubagentChips, { subagent, chatId: 'chat', ...props }),
)

test('an agent helper row is a button that opens its conversation', () => {
  const html = chips({
    a1: { description: 'Review the diff', status: 'done', task_type: 'local_agent' },
    c1: { description: 'Audit login', status: 'running', task_type: '/root/audit_login' },
  })
  assert.equal((html.match(/<button/g) || []).length, 2)
  assert.match(html, /aria-label="Open Review the diff conversation"/)
  assert.match(html, /aria-haspopup="dialog"/)
})

test('a shell task is the command itself, never a helper row', () => {
  assert.equal(chips({ b1: { description: 'npm test', status: 'running', task_type: 'local_bash' } }), '')
  assert.equal(chips({ m1: { description: 'watch logs', status: 'running', task_type: 'monitor' } }), '')
  const mixed = chips({
    b1: { description: 'npm test', status: 'running', task_type: 'local_bash' },
    a1: { description: 'Review the diff', status: 'running', task_type: 'local_agent' },
  })
  assert.doesNotMatch(mixed, /npm test/)
  assert.match(mixed, /Review the diff/)
})

test('older rows without a recorded kind are told apart by their id', () => {
  assert.match(chips({ a49ba48ce4224930a: { description: 'Older agent', status: 'done' } }), /<button/)
  assert.match(chips({ 'call_jPcIEjicMm5z': { description: 'Older Codex helper', status: 'done' } }), /<button/)
  assert.equal(chips({ b0btflf49: { description: 'git status', status: 'done' } }), '')
})

test('without a parent chat a row stays plain status', () => {
  assert.doesNotMatch(chips({ a1: { description: 'x', status: 'done' } }, { chatId: undefined }), /<button/)
})

const working = {
  id: 'delegation:d1:running', status: 'running', task_key: 'audit-login',
  child_chat_id: 'child-1', created_at: 1000, body: '', consumption: 'unknown',
}

test('a working helper row shows its name, engine, current step, and clock', () => {
  const started = new Date(Date.now() - 75_000).toISOString()
  const html = renderWithModels(React.createElement(HelperResultCard, {
    chatId: 'chat',
    event: {
      ...working, delegation_id: 'd1', provider: 'codex', model: 'gpt-6-luna',
      started_at: started, activity: { tool: 'shell', summary: 'npm test' },
    },
  }))
  assert.match(html, /audit-login/)
  assert.match(html, /GPT-6-Luna · Running npm test/)
  assert.match(html, /chat__subagent-elapsed">1m 1[45]s</)
  assert.match(html, /<button[^>]*aria-haspopup="dialog"/)
  assert.match(html, /chat__subagent--running/)
  assert.doesNotMatch(html, /Helper finished/)
})

test('the clock reads the server start time as UTC, whatever the viewer\'s time zone', () => {
  // The server stores UTC without a zone suffix. A fixed non-zero offset (no
  // daylight saving) proves the clock never shifts by the viewer's offset.
  const priorTz = process.env.TZ
  process.env.TZ = 'Asia/Kolkata'
  try {
    const started = new Date(Date.now() - 75_000).toISOString().replace('Z', '')
    const html = renderWithModels(React.createElement(HelperResultCard, {
      chatId: 'chat',
      event: { ...working, delegation_id: 'd1', provider: 'claude', model: 'claude-opus-4-8', started_at: started },
    }))
    assert.match(html, /chat__subagent-elapsed">1m 1[45]s</)
  } finally {
    if (priorTz === undefined) delete process.env.TZ
    else process.env.TZ = priorTz
  }
})

test('a working helper without a known step says it is working', () => {
  const html = renderWithModels(React.createElement(HelperResultCard, {
    chatId: 'chat', event: { ...working, delegation_id: 'd1', provider: 'claude', model: 'claude-opus-4-8' },
  }))
  assert.match(html, /Opus 4.8 · Working/)
})

test('a finished helper keeps its row with its engine and how long it took', () => {
  const html = renderWithModels(React.createElement(HelperResultCard, {
    chatId: 'chat',
    event: { ...working, id: 'delegation:d1:completed', status: 'completed', delegation_id: 'd1',
             provider: 'claude', model: 'claude-opus-4-8', duration_ms: 12_000, body: 'Done.' },
  }))
  assert.match(html, /audit-login/)
  assert.match(html, /Opus 4.8 · Finished/)
  assert.match(html, /12s/)
})

test('a group with working helpers says so instead of claiming they need attention', () => {
  const html = renderWithModels(React.createElement(HelperResultGroupCard, {
    chatId: 'chat',
    events: [working, { ...working, id: 'delegation:d2:completed', status: 'completed' }],
  }))
  assert.match(html, /2 helpers · 1 working · 1 finished/)
  assert.doesNotMatch(html, /need attention/)
})

test('a working Subagents-app helper inside a collapsed activity group counts as running', async () => {
  const { default: ActivityStretch } = await vite.ssrLoadModule('/src/components/ChatView/ActivityStretch.jsx')
  const html = renderWithModels(React.createElement(ActivityStretch, {
    chatId: 'chat',
    surfaceKey: 'm1',
    entries: [
      { idx: 0, item: { type: 'tool', tool: 'Bash', input: 'subagents.py run', status: 'done', tool_use_id: 't1' } },
      { idx: 'h', item: { ...working, delegation_id: 'd1', type: 'helper_result', activityId: working.id } },
    ],
  }))
  assert.match(html, /1 running/)
})

test('an open group draws each helper once, as its launch step, in order', async () => {
  const { default: ActivityStretch } = await vite.ssrLoadModule('/src/components/ChatView/ActivityStretch.jsx')
  const { persistDisclosureOpen, _resetDisclosureStateForTests } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
  _resetDisclosureStateForTests()
  persistDisclosureOpen('chat', 'm2:activity:t0', true)
  const html = renderWithModels(React.createElement(ActivityStretch, {
    chatId: 'chat',
    surfaceKey: 'm2',
    entries: [
      { idx: 0, item: { type: 'tool', tool: 'Bash', input: 'git status', status: 'done', tool_use_id: 't0' } },
      { idx: 1, item: { type: 'tool', tool: 'mcp__mobius_control__spawn_agent', input: 'audit-login', status: 'done', tool_use_id: 't1' } },
      { idx: 'h', item: { ...working, delegation_id: 'd1', type: 'helper_result', activityId: working.id } },
      { idx: 2, item: { type: 'tool', tool: 'Bash', input: 'npm test', status: 'done', tool_use_id: 't2' } },
    ],
  }))
  assert.equal(html.match(/chat__helper-working/g)?.length, 1, 'one row per helper')
  assert.doesNotMatch(html, /Started helper audit-login/, 'the launch step is the row')
  const row = html.indexOf('chat__helper-working')
  assert.ok(html.indexOf('git status') < row && row < html.indexOf('npm test'),
    'the row stands where the helper was launched')
  assert.match(html, /aria-label="audit-login: [^"]*Open its conversation"/)
  _resetDisclosureStateForTests()
})

test('a finished helper stays its launch step: one settled row, counted as done', async () => {
  const { default: ActivityStretch } = await vite.ssrLoadModule('/src/components/ChatView/ActivityStretch.jsx')
  const { persistDisclosureOpen, _resetDisclosureStateForTests } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
  _resetDisclosureStateForTests()
  persistDisclosureOpen('chat', 'm3:activity:t0', true)
  const html = renderWithModels(React.createElement(ActivityStretch, {
    chatId: 'chat',
    surfaceKey: 'm3',
    entries: [
      { idx: 0, item: { type: 'tool', tool: 'Bash', input: 'git status', status: 'done', tool_use_id: 't0' } },
      { idx: 1, item: { type: 'tool', tool: 'mcp__mobius_control__spawn_agent', input: 'audit-login', status: 'done', tool_use_id: 't1' } },
      { idx: 'h', item: { ...working, status: 'completed', duration_ms: 98_000, delegation_id: 'd1', type: 'helper_result', activityId: working.id } },
    ],
  }))
  assert.match(html, /1 done/)
  assert.equal(html.match(/chat__helper-row/g)?.length, 1, 'one row per helper')
  assert.doesNotMatch(html, /Started helper audit-login/)
  assert.match(html, /Finished/)
  _resetDisclosureStateForTests()
})

test('a failed launch stays a failed step; only the retry that started the helper is its row', async () => {
  const { default: ActivityStretch } = await vite.ssrLoadModule('/src/components/ChatView/ActivityStretch.jsx')
  const { persistDisclosureOpen, _resetDisclosureStateForTests } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
  _resetDisclosureStateForTests()
  persistDisclosureOpen('chat', 'm4:activity:t0', true)
  const spawn = (id, extra) => ({ type: 'tool', tool: 'mcp__mobius_control__spawn_agent', input: 'audit-login', status: 'done', tool_use_id: id, ...extra })
  const html = renderWithModels(React.createElement(ActivityStretch, {
    chatId: 'chat',
    surfaceKey: 'm4',
    entries: [
      { idx: 0, item: { type: 'tool', tool: 'Bash', input: 'git status', status: 'done', tool_use_id: 't0' } },
      { idx: 1, item: spawn('t1', { output: "Unknown helper provider 'Claude Code'.", output_exit_code: 1 }) },
      { idx: 2, item: spawn('t2', { output: '{"helper":"audit-login","helper_id":"d1","status":"running"}' }) },
      { idx: 'h', item: { ...working, delegation_id: 'd1', type: 'helper_result', activityId: working.id } },
    ],
  }))
  assert.equal(html.match(/chat__helper-row/g)?.length, 1, 'one row per helper')
  assert.match(html, /1 running/)
  _resetDisclosureStateForTests()
})

test('a relaunch under the same name pairs each launch with the exact helper it started', async () => {
  const { helperLaunches } = await vite.ssrLoadModule('/src/components/ChatView/helperLaunches.js')
  const spawn = (id, helperId) => ({ type: 'tool', tool: 'mcp__mobius_control__spawn_agent', input: 'audit-login', status: 'done', tool_use_id: id, output: `{"helper":"audit-login","helper_id":"${helperId}"}` })
  const first = spawn('t1', 'd1')
  const second = spawn('t2', 'd2')
  const older = { ...working, type: 'helper_result', delegation_id: 'd1', status: 'completed' }
  const newer = { ...working, type: 'helper_result', delegation_id: 'd2' }
  const { rowAt, drawnAtLaunch } = helperLaunches([first, older, second, newer].map((item, idx) => ({ idx, item })))
  assert.equal(rowAt.get(first), older)
  assert.equal(rowAt.get(second), newer)
  assert.equal(drawnAtLaunch.size, 2)
})
