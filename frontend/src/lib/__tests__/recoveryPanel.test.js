import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import RecoveryPanel from '../../components/ErrorBoundary/RecoveryPanel.jsx'

function renderPanel(overrides = {}) {
  return renderToStaticMarkup(createElement(RecoveryPanel, {
    title: 'Something broke',
    subject: 'screen',
    diagnostic: 'Maximum update depth exceeded',
    refreshLabel: 'Refresh screen',
    onRefresh: () => {},
    onAgentRepair: () => {},
    variant: 'boundary',
    ...overrides,
  }))
}

test('recovery panel advances from refresh to agent repair', () => {
  const refresh = renderPanel()
  assert.match(refresh, />Refresh screen</)
  assert.doesNotMatch(refresh, /repair chat|open Recovery in mobius\.you|mobiusctl recovery start/i)

  const agent = renderPanel({ attempt: { phase: 'refreshed' } })
  assert.match(agent, />Refresh again</)
  assert.match(agent, />Start repair chat</)
  assert.doesNotMatch(agent, /open Recovery in mobius\.you|mobiusctl recovery start/i)
})

test('recovery panel distinguishes an active repair from an interrupted one', () => {
  const attempt = {
    phase: 'agent-starting',
    repairRequestId: 'repair-request',
    messageCid: 'repair-message',
  }
  const active = renderPanel({ attempt, repairActive: true })
  assert.match(active, /aria-busy="true"/)
  assert.match(active, /disabled=""[^>]*>Starting repair chat…</)
  assert.doesNotMatch(active, />Refresh again</)

  const interrupted = renderPanel({ attempt })
  assert.match(interrupted, />Resume repair chat</)
  assert.match(interrupted, />Refresh again</)
})

test('recovery panel exposes last resorts only after repair fails', () => {
  const failed = renderPanel({
    attempt: { phase: 'agent-failed', chatId: 'repair/chat' },
  })
  assert.match(failed, />Retry repair chat</)
  assert.match(failed, />Open repair chat</)
  assert.match(failed, /chat=repair%2Fchat/)
  assert.match(failed, /href="https:\/\/www\.mobius\.you\/"/)
  assert.match(failed, /target="_top"/)
  assert.match(failed, /open Recovery in mobius\.you/i)
  assert.match(failed, /<code[^>]*>mobiusctl recovery start<\/code>/)

  const restricted = renderPanel({
    attempt: { phase: 'refreshed' },
    canAskAgent: false,
  })
  assert.doesNotMatch(restricted, /Start repair chat|Retry repair chat/)
  assert.match(restricted, /open Recovery in mobius\.you/i)
  assert.match(restricted, /<code[^>]*>mobiusctl recovery start<\/code>/)
})

test('a directed repair asks the owner to wait instead of linking back to the broken screen', () => {
  const directed = renderPanel({
    attempt: { phase: 'agent-directed', chatId: 'repair/chat' },
    deployment: 'self_hosted',
  })
  assert.match(directed, /repair request was sent/i)
  assert.match(directed, /Give the agent a few minutes/i)
  assert.match(directed, />Refresh again</)
  assert.doesNotMatch(directed, />Open repair chat</)
  assert.match(directed, /This is a self-hosted Möbius instance/)
  assert.doesNotMatch(directed, /mobius\.you/)
})
