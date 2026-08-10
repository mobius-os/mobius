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
  assert.doesNotMatch(refresh, /repair chat|open Recovery in mobius\.you|mobiusctl recovery/i)

  const agent = renderPanel({ attempt: { phase: 'refreshed' } })
  assert.match(agent, />Refresh again</)
  assert.match(agent, />Start repair chat</)
  assert.doesNotMatch(agent, /open Recovery in mobius\.you|mobiusctl recovery/i)
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

test('recovery panel keeps repair actions available after a failure', () => {
  const failed = renderPanel({
    attempt: { phase: 'agent-failed', chatId: 'repair/chat' },
  })
  assert.match(failed, />Retry repair chat</)
  assert.match(failed, />Open repair chat</)
  assert.match(failed, /chat=repair%2Fchat/)
  assert.doesNotMatch(failed, /open Recovery in mobius\.you|mobiusctl recovery/i)

  const restricted = renderPanel({
    attempt: { phase: 'refreshed' },
    canAskAgent: false,
  })
  assert.doesNotMatch(restricted, /Start repair chat|Retry repair chat/)
  assert.match(restricted, /open Möbius directly and ask the agent/i)
})

test('a directed repair keeps the live repair chat reachable while it works', () => {
  // 'agent-directed' is written only once the prompt was delivered, so the
  // chat exists and the agent may be mid-work or waiting on an answer.
  // Telling the owner to wait must never be the only thing they can do.
  const directed = renderPanel({
    attempt: { phase: 'agent-directed', chatId: 'repair/chat' },
  })
  assert.match(directed, /repair request was sent/i)
  assert.match(directed, /Give the agent a few minutes/i)
  assert.match(directed, /open the repair chat to follow along/i)
  assert.match(directed, />Refresh again</)
  assert.match(directed, />Open repair chat</)
  assert.match(directed, /chat=repair%2Fchat/)
})

test('a directed repair with no recorded chat only offers the refresh', () => {
  const directed = renderPanel({ attempt: { phase: 'agent-directed' } })
  assert.match(directed, /repair request was sent/i)
  assert.match(directed, /Give the agent a few minutes/i)
  assert.doesNotMatch(directed, /open the repair chat/i)
  assert.doesNotMatch(directed, />Open repair chat</)
})

test('a failed dispatch does not deny a repair chat it can still link to', () => {
  // The chat is created before the prompt is sent, so a send failure leaves a
  // real chat behind. The copy must not contradict the link beside it.
  const failed = renderPanel({
    attempt: { phase: 'agent-failed', chatId: 'repair/chat' },
  })
  assert.match(failed, /repair request didn’t go through/i)
  assert.doesNotMatch(failed, /couldn’t start/i)
  assert.match(failed, />Open repair chat</)
})
