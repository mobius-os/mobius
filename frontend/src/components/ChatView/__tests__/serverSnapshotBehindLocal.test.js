import test from 'node:test'
import assert from 'node:assert/strict'

import { serverSnapshotBehindLocal } from '../chatRuntimeState.js'

test('server snapshot with completed assistant beats stale user-only cache', () => {
  const local = [{ role: 'user', content: 'build forge', ts: 100 }]
  const server = [
    { role: 'user', content: 'build forge', ts: 200 },
    {
      role: 'assistant',
      content: 'Done',
      ts: 201,
      blocks: [{ type: 'text', content: 'Done' }],
    },
  ]

  assert.equal(serverSnapshotBehindLocal(server, local), false)
})

test('shorter server snapshot is still behind local active turn', () => {
  const local = [
    { role: 'user', content: 'old', ts: 1 },
    { role: 'user', content: 'new', ts: 2, optimistic: true },
  ]
  const server = [{ role: 'user', content: 'old', ts: 1 }]

  assert.equal(serverSnapshotBehindLocal(server, local), true)
})

test('equal-length stale plain cache does not block canonical server row', () => {
  const local = [{ role: 'user', content: 'build forge', ts: 100 }]
  const server = [{ role: 'user', content: 'build forge', ts: 200 }]

  assert.equal(serverSnapshotBehindLocal(server, local), false)
})

test('equal-length explicit optimistic row can outrank server snapshot', () => {
  const local = [{ role: 'user', content: 'fresh', ts: 100, optimistic: true }]
  const server = [{ role: 'user', content: 'old', ts: 90 }]

  assert.equal(serverSnapshotBehindLocal(server, local), true)
})
