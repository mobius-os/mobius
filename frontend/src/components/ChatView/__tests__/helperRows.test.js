/* An agent helper row opens the helper's own conversation; a shell task row does not. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: SubagentChips } = await vite.ssrLoadModule('/src/components/ChatView/SubagentChips.jsx')
after(() => vite.close())

const chips = (subagent, props = {}) => renderToStaticMarkup(
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

test('a background shell command row has no conversation to open', () => {
  const html = chips({ b1: { description: 'npm test', status: 'running', task_type: 'local_bash' } })
  assert.doesNotMatch(html, /<button/)
  assert.match(html, /npm test/)
})

test('older rows without a recorded kind are told apart by their id', () => {
  assert.match(chips({ a49ba48ce4224930a: { description: 'Older agent', status: 'done' } }), /<button/)
  assert.match(chips({ 'call_jPcIEjicMm5z': { description: 'Older Codex helper', status: 'done' } }), /<button/)
  assert.doesNotMatch(chips({ b0btflf49: { description: 'git status', status: 'done' } }), /<button/)
})

test('without a parent chat a row stays plain status', () => {
  assert.doesNotMatch(chips({ a1: { description: 'x', status: 'done' } }, { chatId: undefined }), /<button/)
})
