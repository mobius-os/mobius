import test from 'node:test'
import assert from 'node:assert/strict'

import { providerAllowance } from '../../components/SettingsView/providerUsage.js'


test('plan providers follow typed weekly meaning, not display labels or other limits', () => {
  assert.deepEqual(providerAllowance({
    state: 'ready',
    windows: [
      { kind: 'other', label: 'Weekly', used_percent: 20 },
      { kind: 'weekly', label: 'Renamed allowance', used_percent: 58 },
      { kind: 'other', label: 'Extra usage', used_percent: 75 },
    ],
  }), { kind: 'weekly', label: 'Weekly usage', usedPercent: 58 })
  assert.deepEqual(providerAllowance({ state: 'ready', windows: [] }), {
    kind: 'weekly', label: 'Weekly usage', usedPercent: null,
  })
})
