/* The owner's agent network names a helper by its title; agents keep its name as the address. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
const vite = await createSsrRenderServer()
const { default: AgentCoordinationFeed } = await vite.ssrLoadModule('/src/components/Agents/AgentCoordinationFeed.jsx')
after(() => vite.close())

test('a live helper reads as its title, a lead as its name', () => {
  const html = renderToStaticMarkup(React.createElement(AgentCoordinationFeed, {
    snapshot: {
      scope: { kind: 'delegation', id: 'goal' },
      peers: [
        { id: 'lead', name: 'Release lead', role: 'lead', online: true },
        { id: 'helper', name: 'verify-build', title: 'Checking the release build', role: 'helper', online: true },
      ],
      messages: [],
    },
  }))
  assert.match(html, /title="Release lead, Checking the release build"/)
  assert.doesNotMatch(html, /verify-build/)
})
