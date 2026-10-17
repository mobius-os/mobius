/* Disconnected providers remain below the reorderable connected prefix. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { connectedProvidersFirst, moveConnectedProvider, hasActiveConnectedProvider } from '../backgroundProviderOrder.js'

const rows = [
  { provider: 'claude' },
  { provider: 'codex' },
  { provider: 'mobius' },
]

test('connected providers lead while disconnected providers retain their relative order', () => {
  const connected = new Set(['codex', 'mobius'])
  assert.deepEqual(
    connectedProvidersFirst(rows, connected).map(row => row.provider),
    ['codex', 'mobius', 'claude'],
  )
  assert.deepEqual(rows.map(row => row.provider), ['claude', 'codex', 'mobius'])
})

test('reordering cannot cross into the disconnected suffix', () => {
  const connected = new Set(['codex', 'mobius'])
  assert.deepEqual(
    moveConnectedProvider(rows, 0, 1, connected).map(row => row.provider),
    ['mobius', 'codex', 'claude'],
  )
  assert.equal(moveConnectedProvider(rows, 0, 2, connected), null)
  assert.equal(moveConnectedProvider(rows, 2, 0, connected), null)
})

test('connecting and disconnecting move rows across the boundary without losing order', () => {
  assert.deepEqual(
    connectedProvidersFirst(rows, new Set(['mobius'])).map(row => row.provider),
    ['mobius', 'claude', 'codex'],
  )
  assert.deepEqual(
    connectedProvidersFirst(rows, new Set()).map(row => row.provider),
    ['claude', 'codex', 'mobius'],
  )
})

test('a disconnected enabled row cannot stand in for the last active provider', () => {
  const configured = new Set(['codex'])
  assert.equal(hasActiveConnectedProvider([
    { provider: 'claude', enabled: true },
    { provider: 'codex', enabled: false },
  ], configured), false)
  assert.equal(hasActiveConnectedProvider([
    { provider: 'claude', enabled: true },
    { provider: 'codex', enabled: true },
  ], configured), true)
})
