import test from 'node:test'
import assert from 'node:assert/strict'

import { mostConstrainedRemainingPercent } from '../../components/SettingsView/providerUsage.js'


test('the most constrained usage window is typed data, not a display label', () => {
  assert.equal(mostConstrainedRemainingPercent({
    state: 'ready',
    windows: [
      { label: 'Renamed short window', used_percent: 20 },
      { label: 'Additional allowance', used_percent: 75 },
    ],
  }), 25)
  assert.equal(mostConstrainedRemainingPercent({ state: 'ready', windows: [] }), null)
  assert.equal(mostConstrainedRemainingPercent({ state: 'unavailable' }), null)
})
