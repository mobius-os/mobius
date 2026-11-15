/* A helper reads as the title the server names it by, in the transcript and on the wait row. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderWithModels } from './modelRegistryRender.js'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { helperTitle } from '../helperTitle.js'
import { trayHelpers } from '../goalHelpers.js'
const vite = await createSsrRenderServer()
const { default: HelperResultCard, HelperRowLine } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
after(() => vite.close())

test('the title wins; a missing one only guards with the key', () => {
  assert.equal(helperTitle({ title: ' Fixing the tray ', task_key: 'fix-tray' }), 'Fixing the tray')
  assert.equal(helperTitle({ title: '', task_key: 'fix-tray' }), 'fix-tray')
  assert.equal(helperTitle({}), 'Helper')
})

test('a helper row names the helper by its title', () => {
  const html = renderWithModels(React.createElement(HelperResultCard, {
    chatId: 'chat',
    event: {
      id: 'delegation:d1:running', type: 'helper_result', status: 'completed',
      delegation_id: 'd1', task_key: 'audit-login', title: 'Auditing the login flow',
      provider: 'claude', created_at: 1000,
    },
  }))
  assert.match(html, /Auditing the login flow/)
  assert.doesNotMatch(html, /audit-login/)
})

test('a Goal helper on the wait row reads as its own title, not its plan task', () => {
  const goal = { id: 'goal-a' }
  const { rows } = trayHelpers({ count: 0, items: [], with_goal: { goal_id: 'goal-a', count: 1, items: [
    { id: 'd2', task_key: 'verify-build', title: 'Checking the release build', provider: 'codex', status: 'running' },
  ] } }, goal)
  const html = renderWithModels(React.createElement(HelperRowLine, { event: rows[0], chatId: 'chat', onOpen: () => {} }))
  assert.match(html, /Checking the release build/)
  assert.doesNotMatch(html, /verify-build/)
})
