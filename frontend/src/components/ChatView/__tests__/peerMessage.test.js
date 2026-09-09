import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import {
  effectiveToolName,
  peerMessageLabel,
  toolCallLabel,
  isDistinctiveActivityTool,
} from '../toolActivityLabel.js'
import { peerMessageCardModel } from '../peerMessageCard.js'
import {
  attachToolInput,
  attachToolOutput,
  startToolLifecycle,
} from '../streamReducers.js'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: PeerMessageCard } = await vite.ssrLoadModule(
  '/src/components/ChatView/PeerMessageCard.jsx',
)
const {
  _resetDisclosureStateForTests,
  persistDisclosureOpen,
} = await vite.ssrLoadModule(
  '/src/components/ChatView/disclosureState.js',
)

const priorWindow = globalThis.window
globalThis.window = { location: new URL('https://mobius.test/shell') }
after(() => { globalThis.window = priorWindow; return vite.close() })

function renderCard(peerMessage, { open = false, suffix = 'default' } = {}) {
  const chatId = `peer-message-${suffix}`
  const disclosureKey = `peer-message-${suffix}`
  _resetDisclosureStateForTests()
  if (open) persistDisclosureOpen(chatId, disclosureKey, true)
  return renderToStaticMarkup(React.createElement(PeerMessageCard, {
    t: { type: 'tool', status: 'done', peer_message: peerMessage },
    chatId,
    disclosureKey,
  }))
}

const sentTool = {
  type: 'tool',
  tool: 'mcp__mobius_control__send_agent_message',
  status: 'done',
  peer_message: {
    direction: 'send', status: 'sent', kind: 'handoff',
    peers: ['Aligning local Möbius with upstream'], count: 1,
    broadcast: false,
    body: 'Handoff for brain-token-units.',
  },
}

const receivedTool = {
  type: 'tool',
  tool: 'mcp__mobius_control__read_agent_messages',
  status: 'done',
  peer_message: {
    direction: 'read', status: 'received', count: 1,
    notes: [{ sender: 'Aligning local Möbius with upstream', kind: 'request', body: 'Send the handoff' }],
  },
}

test('peer exchanges retain their identity inside ordinary tool grouping', () => {
  assert.equal(effectiveToolName(sentTool), 'PeerMessage')
  assert.equal(effectiveToolName(receivedTool), 'PeerMessage')
  assert.equal(isDistinctiveActivityTool(sentTool), false)
  // A tool with no marker stays an ordinary block.
  assert.equal(effectiveToolName({ tool: 'Bash' }), 'Bash')
})

test('the label names the peer and the message kind', () => {
  assert.equal(toolCallLabel(sentTool), 'Sent Aligning local Möbius with upstream a handoff')
  assert.equal(toolCallLabel(receivedTool), 'Aligning local Möbius with upstream sent a request')
})

test('labels cover broadcast, multi-peer, empty and failed states', () => {
  assert.equal(
    peerMessageLabel({ peer_message: { direction: 'send', status: 'sent', kind: 'note', broadcast: true } }),
    'Sent the team a note',
  )
  assert.equal(
    peerMessageLabel({ peer_message: { direction: 'send', status: 'sent', kind: 'finding', peers: ['A', 'B'], count: 2 } }),
    'Sent A and B a finding',
  )
  assert.equal(
    peerMessageLabel({ peer_message: { direction: 'send', status: 'sent', kind: 'finding', peers: ['A'], count: 24 } }),
    'Sent A and 23 others a finding',
  )
  assert.equal(
    peerMessageLabel({ peer_message: { direction: 'read', status: 'received', count: 3, notes: [] } }),
    'Received 3 agent messages',
  )
  assert.equal(
    peerMessageLabel({ peer_message: { direction: 'read', status: 'empty' } }),
    'Checked agent messages — none new',
  )
  assert.equal(
    peerMessageLabel({ peer_message: { direction: 'send', status: 'failed' } }),
    'Agent message failed',
  )
  assert.equal(
    peerMessageLabel({ status: 'running', peer_message: { direction: 'send', status: 'sending' } }),
    'Messaging an agent',
  )
})

test('the card model bounds bodies and normalizes an unknown kind', () => {
  const model = peerMessageCardModel({
    direction: 'send', status: 'sent', kind: 'bogus',
    peers: ['A', 'A', 'B'], body: 'y'.repeat(5000),
  })
  assert.equal(model.status, 'sent')
  assert.equal(model.kind, 'note') // unknown kind falls back
  assert.deepEqual(model.peers, ['A', 'B']) // deduped
  assert.equal(model.body.length, 4000) // full note preserved to the platform cap
  assert.equal(model.hasDetail, true)
})

test('an interrupted call is not live and reads as interrupted', () => {
  // A provisional marker left on a DONE tool (owner stopped a long-poll) must
  // settle, not spin forever.
  const pm = { direction: 'read', status: 'reading' }
  assert.equal(
    peerMessageLabel({ status: 'done', peer_message: pm }),
    'Agent message interrupted',
  )
  const markup = renderCard(pm, { suffix: 'interrupted' })
  assert.doesNotMatch(markup, /chat__tool-icon--running/)
  assert.doesNotMatch(markup, /…/)
  assert.match(markup, /Agent message interrupted/)
})

test('a malformed marker falls back to the generic tool block', () => {
  assert.equal(peerMessageCardModel({ status: 'weird' }), null)
  assert.equal(
    effectiveToolName({ tool: 'Bash', peer_message: { status: 'weird' } }),
    'Bash',
  )
})

test('a failed marker without a valid direction is not a peer card', () => {
  // The failed branch must not bypass the direction guard.
  assert.equal(peerMessageCardModel({ status: 'failed' }), null)
  assert.equal(peerMessageCardModel({ status: 'failed', direction: 'sideways' }), null)
  assert.equal(
    effectiveToolName({ tool: 'Bash', peer_message: { status: 'failed' } }),
    'Bash',
  )
  // A properly-directed failure still classifies.
  assert.equal(
    peerMessageCardModel({ status: 'failed', direction: 'send' }).status,
    'failed',
  )
})

test('a failed call renders its bounded reason', () => {
  const pm = { direction: 'send', status: 'failed', reason: 'recipient is unavailable' }
  const model = peerMessageCardModel(pm)
  assert.equal(model.reason, 'recipient is unavailable')
  assert.equal(model.hasDetail, true)
  assert.match(renderCard(pm, { open: true, suffix: 'failed-reason' }), /recipient is unavailable/)
})

test('a truncated body shows an excerpt notice', () => {
  const pm = {
    direction: 'send', status: 'sent', kind: 'note',
    peers: ['A'], count: 1, body: 'x', body_truncated: true,
  }
  assert.equal(peerMessageCardModel(pm).bodyTruncated, true)
  assert.match(renderCard(pm, { open: true, suffix: 'excerpt' }), /Excerpt/)
})

test('live start, input and completed-output reducers preserve the marker', () => {
  let items = startToolLifecycle([], {
    tool: 'mobius_control:read_agent_messages',
    tool_use_id: 'read-1',
    peer_message: { direction: 'read', status: 'reading' },
  })
  assert.equal(items[0].peer_message.status, 'reading')

  items = attachToolInput(items, {
    tool_use_id: 'read-1',
    input: '{}',
    peer_message: { direction: 'read', status: 'reading' },
  })
  items = attachToolOutput(items, '{"messages":[]}', {
    tool_use_id: 'read-1',
    output_complete: true,
    peer_message: { direction: 'read', status: 'empty' },
  })
  assert.equal(items[0].peer_message.status, 'empty')
})

test('empty and failed statuses have no empty disclosure control', () => {
  for (const status of ['reading', 'empty', 'failed']) {
    const peerMessage = { direction: 'read', status }
    assert.equal(peerMessageCardModel(peerMessage).hasDetail, false)
    const markup = renderCard(peerMessage, { suffix: status })
    assert.doesNotMatch(markup, /<button/)
    assert.doesNotMatch(markup, /role="region"/)
    assert.match(markup, /chat__tool-header--static/)
  }
})

test('a settled note disclosure shows the complete bounded body', () => {
  const body = 'First line\nSecond line remains visible.'
  const model = peerMessageCardModel({ ...sentTool.peer_message, body })
  assert.equal(model.body, body)
  assert.equal(model.hasDetail, true)
  const closed = renderCard({ ...sentTool.peer_message, body }, {
    suffix: 'closed',
  })
  assert.match(closed, /<button/)
  assert.match(closed, /aria-controls=/)
  assert.match(closed, /hidden=""/)

  const open = renderCard({ ...sentTool.peer_message, body }, {
    open: true,
    suffix: 'open',
  })
  assert.match(open, /role="region"/)
  assert.match(open, /First line\nSecond line remains visible\./)
})

test('the card model returns null for an unrecognized shape', () => {
  assert.equal(peerMessageCardModel(null), null)
  assert.equal(peerMessageCardModel({ status: 'weird' }), null)
})

test('incoming timeline messages expose full inline text, time, and optional source navigation', () => {
  const chatId = 'inline-message'
  const disclosureKey = 'inline-note'
  _resetDisclosureStateForTests()
  persistDisclosureOpen(chatId, disclosureKey, true)
  const body = 'Keep working independently.\n<script>not markup</script>'
  const html = renderToStaticMarkup(React.createElement(PeerMessageCard, {
    t: { status: 'done', peer_message: { direction: 'read', status: 'received', count: 1,
      notes: [{ sender: 'Other agent', kind: 'finding', body }] } },
    chatId, disclosureKey,
    records: [{ sender_chat_id: 'other', sender_name: 'Other agent', created_at: '2026-09-08T12:17:00', observedDelivery: 'during_work' }],
  }))
  assert.match(html, /Received from Other agent/)
  assert.match(html, /Delivered during work/)
  assert.match(html, /2026-09-08T12:17:00.000Z/)
  assert.match(html, /Keep working independently/)
  assert.doesNotMatch(html, /<script|&lt;script&gt;/)
  assert.match(html, /href="\/shell\?chat=other"/)
  assert.match(html, /aria-expanded="true"/)
})

test('requested delivery never claims the other agent read a message', () => {
  _resetDisclosureStateForTests()
  persistDisclosureOpen('delivery', 'note', true)
  const html = renderToStaticMarkup(React.createElement(PeerMessageCard, {
    t: sentTool, chatId: 'delivery', disclosureKey: 'note',
    records: [{ delivery: 'interrupt', recipient_chat_id: 'other' }],
  }))
  assert.match(html, /Immediate delivery requested · not a read receipt/)
  assert.doesNotMatch(html, /Delivered during work/)
})

test('peer disclosure follows tool chrome and renders structured message prose', () => {
  const html = renderCard({ ...sentTool.peer_message,
    body: '## Decision\n\nUse **the existing renderer**.\n\n- Keep `stable-id`\n- Keep the timeline\n\n```text\n<script>quoted, not executed</script>\n```',
  }, { open: true, suffix: 'markdown' })
  assert.equal((html.split('</button>')[0].match(/<svg/g) || []).length, 1, 'only the direction icon, no trailing disclosure chevron')
  assert.match(html, /aria-expanded="true"/)
  assert.match(html, /<h2[^>]*>Decision<\/h2>/)
  assert.match(html, /<strong>the existing renderer<\/strong>/)
  assert.match(html, /<ul/)
  assert.match(html, /<code[^>]*>stable-id<\/code>/)
  assert.match(html, /&lt;script&gt;quoted, not executed&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script/)
})


test('single received note names its sender once; grouped notes retain each sender', () => {
  const note = { sender: 'Review agent', kind: 'finding', body: 'Verified.' }
  const one = renderCard({ direction: 'read', status: 'received', count: 1, notes: [note] }, { open: true, suffix: 'single-sender' })
  assert.doesNotMatch(one, /chat__peer-kicker|chat__peer-from/)
  assert.match(one, /Received from Review agent/)
  assert.match(one, /chat__peer-kind--finding/)
  const many = renderCard({ direction: 'read', status: 'received', count: 2,
    notes: [note, { ...note, sender: 'Build agent' }] }, { open: true, suffix: 'multiple-senders' })
  assert.match(many, /chat__peer-kicker/)
  assert.equal((many.match(/chat__peer-from/g) || []).length, 2)
  assert.match(many, /from Review agent/)
  assert.match(many, /from Build agent/)
})
