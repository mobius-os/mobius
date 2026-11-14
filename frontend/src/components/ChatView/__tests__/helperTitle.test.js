/* A helper reads as its title, else its task key made readable, never the raw key. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderWithModels } from './modelRegistryRender.js'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { helperTitle, humanizeTaskKey } from '../helperTitle.js'
const vite = await createSsrRenderServer()
const { default: HelperResultCard } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
after(() => vite.close())

test('the title wins and an untitled helper reads as its humanized task key', () => {
  assert.equal(helperTitle({ title: ' Fixing the tray ', task_key: 'fix-tray' }), 'Fixing the tray')
  assert.equal(helperTitle({ title: '', task_key: 'fix-tray.close_v2' }), 'Fix tray close v2')
  assert.equal(humanizeTaskKey('--'), '')
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
