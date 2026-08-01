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
  assert.doesNotMatch(refresh, /repair chat|isolated recovery workspace/i)

  const agent = renderPanel({ attempt: { phase: 'refreshed' } })
  assert.match(agent, />Refresh again</)
  assert.match(agent, />Start repair chat</)
  assert.doesNotMatch(agent, /isolated recovery workspace/i)
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
  assert.match(failed, /isolated recovery workspace/i)

  const restricted = renderPanel({
    attempt: { phase: 'refreshed' },
    canAskAgent: false,
  })
  assert.doesNotMatch(restricted, /Start repair chat|Retry repair chat/)
  assert.match(restricted, /isolated recovery workspace/i)
})
