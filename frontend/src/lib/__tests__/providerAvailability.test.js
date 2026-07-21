import test from 'node:test'
import assert from 'node:assert/strict'
import {
  PROVIDER_AVAILABILITY_PHASE,
  configuredProviderSet,
  providerAvailabilityNeedsAttention,
  resolveProviderAvailability,
  visibleProviderModels,
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
  const configured = configuredProviderSet({
    codex: { configured: true, authenticated: true },
    claude: { authenticated: false },
    legacy: { authenticated: true },
    contradictory: { configured: false, authenticated: true },
    future: {},
  })

  assert.deepEqual([...configured], ['codex', 'legacy'])
})

test('an unavailable retained provider exposes only its selected model', () => {
  const models = [{ id: 'one' }, { id: 'two' }]
  const configured = new Set(['codex'])

  assert.deepEqual(visibleProviderModels('codex', models, configured), models)
  assert.deepEqual(
    visibleProviderModels('claude', models, configured, 'claude', 'two'),
    [{ id: 'two' }],
  )
  assert.deepEqual(visibleProviderModels('future', models, configured, 'claude', 'two'), [])
})

test('attention means status failure or no configured provider, not optional disconnects', () => {
  assert.equal(providerAvailabilityNeedsAttention({
    phase: PROVIDER_AVAILABILITY_PHASE.ERROR,
    configuredProviders: new Set(),
  }), true)
  assert.equal(providerAvailabilityNeedsAttention({
    phase: PROVIDER_AVAILABILITY_PHASE.READY,
    configuredProviders: new Set(),
  }), true)
  assert.equal(providerAvailabilityNeedsAttention({
    phase: PROVIDER_AVAILABILITY_PHASE.READY,
    configuredProviders: new Set(['codex']),
  }), false)
})
