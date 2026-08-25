import test from 'node:test'
import assert from 'node:assert/strict'

import {
  providerAllowance,
  providerAllowanceSummary,
} from '../../components/SettingsView/providerUsage.js'


test('plan providers follow typed weekly meaning, not display labels or other limits', () => {
  assert.deepEqual(providerAllowance('codex', {
    state: 'ready',
    windows: [
      { kind: 'other', label: 'Weekly', used_percent: 20 },
      { kind: 'weekly', label: 'Renamed allowance', used_percent: 58 },
      { kind: 'other', label: 'Extra usage', used_percent: 75 },
    ],
  }), {
    kind: 'weekly', label: 'Weekly usage', usedPercent: 58, expiresAt: null,
  })
  assert.deepEqual(providerAllowance('claude', { state: 'ready', windows: [] }), {
    kind: 'weekly', label: 'Weekly usage', usedPercent: null, expiresAt: null,
  })
})

test('Möbius follows typed API-credit usage instead of weekly windows', () => {
  assert.deepEqual(providerAllowance('mobius', {
    state: 'ready',
    windows: [
      { kind: 'weekly', used_percent: 80 },
      {
        kind: 'api_credits',
        used_percent: 2.5,
        expires_at: '2026-09-07T19:40:34.682998+00:00',
      },
    ],
  }), {
    kind: 'api_credits',
    label: 'API credits usage',
    usedPercent: 2.5,
    expiresAt: '2026-09-07T19:40:34.682998+00:00',
  })
})

test('Möbius allowance copy matches the consumed brain gauge at integer precision', () => {
  const now = new Date('2026-08-25T20:00:00Z')
  assert.equal(providerAllowanceSummary('mobius', {
    usedPercent: 0.0163,
    expiresAt: '2026-09-07T19:40:34.682998+00:00',
  }, now), '0% used · 13d left')
  assert.equal(providerAllowanceSummary('mobius', {
    usedPercent: 2.5,
    expiresAt: '2026-09-07T19:40:34.682998+00:00',
  }, now), '3% used · 13d left')
})
