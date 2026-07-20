import test from 'node:test'
import assert from 'node:assert/strict'
import {
  PROVIDER_AVAILABILITY_PHASE,
  connectedProviderSet,
  resolveProviderAvailability,
  shouldShowProvider,
} from '../providerAvailability.js'

test('availability has explicit loading, ready, and error phases', () => {
  assert.equal(
    resolveProviderAvailability({ data: undefined, isError: false }).phase,
    PROVIDER_AVAILABILITY_PHASE.LOADING,
  )
  assert.equal(
    resolveProviderAvailability({ data: {}, isError: false }).phase,
    PROVIDER_AVAILABILITY_PHASE.READY,
  )
  assert.equal(
    resolveProviderAvailability({ data: undefined, isError: true }).phase,
    PROVIDER_AVAILABILITY_PHASE.ERROR,
  )
})

test('configured is authoritative with authenticated as a legacy fallback', () => {
  const connected = connectedProviderSet({
    codex: { configured: true, authenticated: true },
    claude: { authenticated: false },
    legacy: { authenticated: true },
    contradictory: { configured: false, authenticated: true },
    future: {},
  })

  assert.deepEqual([...connected], ['codex', 'legacy'])
})

test('provider filtering is fail-closed and supports a retained selection', () => {
  const connected = new Set(['codex', 'future'])

  assert.equal(shouldShowProvider('codex', connected), true)
  assert.equal(shouldShowProvider('future', connected), true)
  assert.equal(shouldShowProvider('claude', connected), false)
  assert.equal(shouldShowProvider('claude', connected, 'claude'), true)
})
