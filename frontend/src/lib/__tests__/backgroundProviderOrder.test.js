/* The owner's saved priority survives a temporary disconnect; display grouping is never saved. */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  connectedProvidersFirst,
  disablesLastConnectedProvider,
  hasActiveConnectedProvider,
  moveConnectedProvider,
  rowsAfterConnect,
} from '../backgroundProviderOrder.js'

const rows = [
  { provider: 'claude', enabled: true },
  { provider: 'codex', enabled: true },
  { provider: 'mobius', enabled: true },
]
const names = list => list.map(row => row.provider)

test('display lists connected providers first without changing the saved order', () => {
  const connected = new Set(['codex', 'mobius'])
  assert.deepEqual(names(connectedProvidersFirst(rows, connected)), ['codex', 'mobius', 'claude'])
  assert.deepEqual(names(rows), ['claude', 'codex', 'mobius'])
})

test('reordering while a provider is signed out keeps its saved slot for reconnect', () => {
  // Claude's token expired; the owner swaps Codex and Möbius on screen.
  const connected = new Set(['codex', 'mobius'])
  const saved = moveConnectedProvider(rows, 0, 1, connected)
  assert.deepEqual(names(saved), ['claude', 'mobius', 'codex'])
  // Reconnecting Claude restores it to the top of the priority it had.
  assert.deepEqual(names(connectedProvidersFirst(saved, new Set(['claude', 'codex', 'mobius']))), ['claude', 'mobius', 'codex'])
})

test('reordering uses display positions and cannot cross into the disconnected group', () => {
  const connected = new Set(['claude', 'mobius'])
  // Display: claude, mobius | codex. Move mobius above claude.
  assert.deepEqual(names(moveConnectedProvider(rows, 1, 0, connected)), ['mobius', 'codex', 'claude'])
  assert.equal(moveConnectedProvider(rows, 0, 2, connected), null)
  assert.equal(moveConnectedProvider(rows, 2, 0, connected), null)
  assert.equal(moveConnectedProvider(rows, 1, 1, connected), null)
})

test('disabling the last connected provider is refused even while a disconnected row stays enabled', () => {
  const configured = new Set(['codex'])
  const current = [
    { provider: 'claude', enabled: true },
    { provider: 'codex', enabled: true },
  ]
  const next = current.map(row => row.provider === 'codex' ? { ...row, enabled: false } : row)
  assert.equal(hasActiveConnectedProvider(next, configured), false)
  assert.equal(disablesLastConnectedProvider(current, next, configured), true)
})

test('disabling one of two active connected providers is allowed', () => {
  const configured = new Set(['claude', 'codex'])
  const next = rows.map(row => row.provider === 'claude' ? { ...row, enabled: false } : row)
  assert.equal(disablesLastConnectedProvider(rows, next, configured), false)
})

test('edits stay possible after the only active provider was signed out', () => {
  // Codex was the only active provider and is now disconnected, so no edit
  // can "disable the last one"; the owner must still be able to re-enable.
  const configured = new Set(['claude'])
  const current = [
    { provider: 'claude', enabled: false },
    { provider: 'codex', enabled: true },
  ]
  const enableClaude = current.map(row => row.provider === 'claude' ? { ...row, enabled: true } : row)
  assert.equal(disablesLastConnectedProvider(current, enableClaude, configured), false)
  assert.equal(disablesLastConnectedProvider(current, [...current].reverse(), configured), false)
})

test('connecting Gemini leaves it out of background work; other providers join', () => {
  const saved = [{ provider: 'claude', enabled: true }, { provider: 'gemini', enabled: false }, { provider: 'codex', enabled: false }]
  const gemini = rowsAfterConnect(saved, { provider: 'gemini', model: 'gemini-3.8-flash', effort: 'medium' }, { first: false })
  assert.deepEqual(gemini.map(row => [row.provider, row.enabled]), [['claude', true], ['gemini', false], ['codex', false]])
  assert.equal(gemini[1].model, 'gemini-3.8-flash')
  const codex = rowsAfterConnect(saved, { provider: 'codex', model: 'gpt', effort: 'medium' }, { first: false })
  assert.deepEqual(codex.map(row => [row.provider, row.enabled]), [['claude', true], ['gemini', false], ['codex', true]])
})

test('Gemini as the first provider still runs background work, which needs one', () => {
  const next = rowsAfterConnect([{ provider: 'claude', enabled: true }], { provider: 'gemini', model: 'm' }, { first: true })
  assert.deepEqual(next.map(row => [row.provider, row.enabled]), [['gemini', true], ['claude', false]])
})
