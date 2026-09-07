import assert from 'node:assert/strict'
import test from 'node:test'

import {
  groupPeerMessages,
  peerNetworkStatus,
  scopeLabel,
  targetLabel,
} from '../../Agents/agentNetworkModel.js'

test('stable send identity groups recipients without guessing from timing', () => {
  const grouped = groupPeerMessages([
    {
      id: 'one', send_id: 'send-1', sender_chat_id: 'sender',
      recipient_name: 'Builder', body: 'Same note', created_at: '2026-09-01T10:00:00Z',
    },
    {
      id: 'two', send_id: 'send-1', sender_chat_id: 'sender',
      recipient_name: 'Reviewer', body: 'Same note', created_at: '2026-09-01T10:01:00Z',
    },
  ])
  assert.equal(grouped.length, 1)
  assert.deepEqual(grouped[0].recipient_names, ['Builder', 'Reviewer'])
  assert.equal(targetLabel(grouped[0], 'Goal scope'), '2 agents')
})

test('network status distinguishes local scope from global connectivity', () => {
  const snapshot = {
    scope: { kind: 'delegation', id: 'goal' },
    scope_peer_ids: ['lead', 'helper'],
    peers: [
      { id: 'lead', online: true },
      { id: 'helper', online: true },
      { id: 'outside', online: true },
    ],
    peer_total: 3,
    peers_truncated: false,
  }
  assert.equal(scopeLabel(snapshot), 'Goal scope')
  assert.equal(peerNetworkStatus(snapshot), '2 here · 3 connected')
  assert.equal(
    targetLabel({ broadcast: true, recipient_names: ['Scope'] }, scopeLabel(snapshot)),
    'Goal scope',
  )
})
