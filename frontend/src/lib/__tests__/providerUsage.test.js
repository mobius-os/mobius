/* Usage snapshots stay bounded and format reset times without JSX mounting. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  clampUsagePercent,
  formatPlanStatus,
  formatUsagePercent,
  formatTrialTimeLeft,
  formatUsageReset,
  visibleUsageWindows,
} from '../../components/SettingsView/providerUsage.js'

const usageView = readFileSync(
  new URL('../../components/SettingsView/ProviderUsage.jsx', import.meta.url),
  'utf8',
)
const settingsView = readFileSync(
  new URL('../../components/SettingsView/SettingsView.jsx', import.meta.url),
  'utf8',
)
const providerCss = readFileSync(
  new URL('../../components/ProviderAuth/ProviderAuth.css', import.meta.url),
  'utf8',
)

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

test('trial time remaining stays compact and is derived from the exact expiry', () => {
  assert.equal(
    formatTrialTimeLeft(
      '2026-09-07T20:00:00Z',
      new Date('2026-08-25T20:00:01Z'),
    ),
    '13d left',
  )
  assert.equal(formatTrialTimeLeft(
    '2026-08-25T19:59:59Z',
    new Date('2026-08-25T20:00:00Z'),
  ), 'Ended')
  assert.equal(formatTrialTimeLeft('not-a-date'), '')
})

test('reset formatting distinguishes today from another day', () => {
  const now = new Date(2026, 6, 30, 12, 0)
  const today = formatUsageReset(new Date(2026, 6, 30, 17, 5), now)
  const later = formatUsageReset(new Date(2026, 7, 3, 7, 0), now)

  assert.equal(today, 'Resets 17:05')
  assert.equal(later, 'Resets Mon 07:00')
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

test('Claude and Codex usage disclosures stay independently expandable', () => {
  assert.match(settingsView, /expandedUsage\.codex/)
  assert.match(settingsView, /expandedUsage\.claude/)
  assert.match(
    settingsView,
    /setExpandedUsage\(prev => \(\{ \.\.\.prev, codex: !prev\.codex \}\)\)/,
  )
  assert.match(
    settingsView,
    /setExpandedUsage\(prev => \(\{ \.\.\.prev, claude: !prev\.claude \}\)\)/,
  )
  assert.doesNotMatch(settingsView, /expandedUsage === '(?:codex|claude)'/)
})

test('expanded usage shares aligned columns without tall cards', () => {
  assert.match(usageView, /className="provider-usage__track"/)
  assert.match(usageView, /role="progressbar"/)
  assert.match(usageView, /className="provider-usage__fill"/)
  assert.match(
    providerCss,
    /\.provider-usage__track\s*\{[^}]*height:\s*3px;/s,
  )
  assert.match(
    providerCss,
    /\.provider-usage__windows\s*\{[^}]*grid-template-columns:/s,
  )
  assert.match(
    providerCss,
    /\.provider-usage__window\s*\{[^}]*display:\s*contents;/s,
  )
})
