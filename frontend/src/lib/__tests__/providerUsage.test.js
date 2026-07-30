/* Usage snapshots stay bounded and format reset times without JSX mounting. */

import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clampUsagePercent,
  formatPlanStatus,
  formatUsagePercent,
  formatUsageReset,
  visibleUsageWindows,
} from '../../components/SettingsView/providerUsage.js'

test('connected plan status uses the compact green-disclosure copy', () => {
  assert.equal(formatPlanStatus('Max plan'), 'Plan: Max')
  assert.equal(formatPlanStatus('Pro plan'), 'Plan: Pro')
  assert.equal(formatPlanStatus('API billing'), 'Plan: API billing')
  assert.equal(formatPlanStatus(''), 'Plan: Unknown')
})

test('usage percentages are bounded and retain useful precision', () => {
  assert.equal(clampUsagePercent(-2), 0)
  assert.equal(clampUsagePercent(140), 100)
  assert.equal(formatUsagePercent(34.2), '34.2')
  assert.equal(formatUsagePercent(54), '54')
})

test('reset formatting distinguishes today from another day', () => {
  const now = new Date('2026-07-30T12:00:00Z')
  const today = formatUsageReset('2026-07-30T17:00:00Z', now)
  const later = formatUsageReset('2026-08-03T17:00:00Z', now)

  assert.match(today, /^resets /)
  assert.doesNotMatch(today, /Mon/)
  assert.match(later, /^resets Mon /)
})

test('only four valid allowance windows are rendered', () => {
  const windows = visibleUsageWindows({
    windows: [
      { id: 'a', label: 'A' },
      null,
      { id: 'b', label: 'B' },
      { id: 'c', label: 'C' },
      { id: 'd', label: 'D' },
      { id: 'e', label: 'E' },
    ],
  })

  assert.deepEqual(windows.map(window => window.id), ['a', 'b', 'c', 'd'])
})
