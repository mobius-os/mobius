/* Usage snapshots stay bounded and format reset times without JSX mounting. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { settingsQueries } from '../../hooks/queries.js'
import ProviderUsage from '../../components/SettingsView/ProviderUsage.jsx'
import {
  clampUsagePercent,
  formatPlanStatus,
  formatUsagePercent,
  formatTrialTimeLeft,
  formatUsageReset,
  formatUsageObservedAt,
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
const shellSource = readFileSync(
  new URL('../../components/Shell/Shell.jsx', import.meta.url),
  'utf8',
)
const paneChatSource = readFileSync(
  new URL('../../components/Shell/PaneChatView.jsx', import.meta.url),
  'utf8',
)
const embeddedChatSource = readFileSync(
  new URL('../../components/ChatEmbed/ChatEmbed.jsx', import.meta.url),
  'utf8',
)

test('account change clears only that provider reading before refetch', async () => {
  const client = new QueryClient()
  client.setQueryData(settingsQueries.providerUsage.keyFor('codex'), {
    state: 'ready', windows: [{ used_percent: 100 }],
  })
  client.setQueryData(settingsQueries.providerUsage.keyFor('claude'), {
    state: 'ready', windows: [{ used_percent: 80 }],
  })
  await settingsQueries.providerUsage.reset(client, 'codex')
  assert.equal(client.getQueryData(settingsQueries.providerUsage.keyFor('codex')), undefined)
  assert.equal(
    client.getQueryData(settingsQueries.providerUsage.keyFor('claude')).windows[0].used_percent,
    80,
  )
  assert.match(shellSource, /model_providers_changed[\s\S]*providerUsage\.reset\(queryClient, ev\.provider\)/)
})

test('Brain usage refetches when a chat mounts and on window focus', () => {
  assert.match(paneChatSource, /<ChatView\s+key=\{chatId\}/)
  assert.match(embeddedChatSource, /<ChatView\s+key=\{chatId\}/)
  const querySource = readFileSync(new URL('../../hooks/queries.js', import.meta.url), 'utf8')
  assert.match(querySource, /function useProviderUsageQuery[\s\S]*staleTime: 0,[\s\S]*refetchOnWindowFocus: true/)
})

for (const provider of ['codex', 'claude']) {
  const otherProvider = provider === 'codex' ? 'claude' : 'codex'
  test(`${provider} allowance event clears only that provider and refetches open Brain observers`, async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const key = settingsQueries.providerUsage.keyFor(provider)
    const reading = percent => ({ state: 'ready', windows: [{ used_percent: percent }] })
    client.setQueryData(key, reading(100))
    client.setQueryData(settingsQueries.providerUsage.keyFor(otherProvider), reading(80))
    let finishOld
    const oldReading = new Promise(resolve => { finishOld = resolve })
    let probes = 0
    const observer = new QueryObserver(client, {
      queryKey: key,
      queryFn: () => ++probes === 1 ? oldReading : Promise.resolve(reading(7)),
      staleTime: 0,
    })
    const unsubscribe = observer.subscribe(() => {})
    try {
      assert.equal(probes, 1)
      await settingsQueries.providerUsage.reset(client, provider)
      finishOld(reading(100))
      await oldReading
      assert.equal(probes, 2)
      assert.equal(observer.getCurrentResult().data.windows[0].used_percent, 7)
      assert.equal(client.getQueryData(settingsQueries.providerUsage.keyFor(otherProvider)).windows[0].used_percent, 80)
      assert.ok(
        /ev\.type === 'provider_usage_changed'[\s\S]*?providerUsage\.reset\(queryClient, ev\.provider\)/.test(shellSource),
        'Shell must clear the affected provider on the usage-change event',
      )
    } finally {
      unsubscribe()
      client.clear()
    }
  })
}

test('connected plan status uses the compact green-disclosure copy', () => {
  assert.equal(formatPlanStatus('Max plan'), 'Plan: Max')
  assert.equal(formatPlanStatus('Pro plan'), 'Plan: Pro')
  assert.equal(formatPlanStatus('API billing'), 'Plan: API billing')
  assert.equal(formatPlanStatus(''), 'Plan: Unknown')
  assert.equal(formatPlanStatus('Self Serve Business Prolite plan'), 'Plan: Business')
})

test('usage freshness renders a verified reading time, stale state, and refresh action', () => {
  assert.equal(formatUsageObservedAt('bad date'), '')
  const snapshot = {
    state: 'ready',
    windows: [{ id: 'weekly', label: 'Weekly', used_percent: 27 }],
    observed_at: '2026-10-06T09:07:01Z',
    stale: true,
  }
  const html = renderToStaticMarkup(createElement(ProviderUsage, {
    id: 'provider-usage-codex', snapshot, onRefresh: () => {},
  }))
  assert.match(html, /Last available reading · Checked/)
  assert.match(html, />Refresh<\/button>/)
  const refreshing = renderToStaticMarkup(createElement(ProviderUsage, {
    id: 'provider-usage-codex', snapshot, refreshing: true, onRefresh: () => {},
  }))
  assert.match(refreshing, /disabled=""[^>]*>Refreshing…<\/button>/)
  const unavailable = renderToStaticMarkup(createElement(ProviderUsage, {
    id: 'provider-usage-claude', failed: true, onRefresh: () => {},
  }))
  assert.match(unavailable, /Could not refresh/)
  assert.doesNotMatch(unavailable, /Time unavailable/)
  assert.match(settingsView, /onRefresh=\{\(\) => codexUsageQuery\.refetch\(\)\}/)
  assert.match(settingsView, /onRefresh=\{\(\) => claudeUsageQuery\.refetch\(\)\}/)
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
  const today = formatUsageReset(new Date(2026, 6, 30, 17, 5))
  const later = formatUsageReset(new Date(2026, 7, 3, 7, 0))

  assert.equal(today, 'Resets 30th Jul 2026, 17:05')
  assert.equal(later, 'Resets 3rd Aug 2026, 07:00')
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

test('dedicated provider pages fetch only their own usage', () => {
  assert.match(settingsView, /selectedProvider === 'codex'/)
  assert.match(settingsView, /selectedProvider === 'claude'/)
  assert.match(settingsView, /selectedProvider && selectedProvider !== row\.provider \? null/)
  assert.match(settingsView, /snapshot=\{codexUsageQuery\.data\}/)
  assert.match(settingsView, /snapshot=\{claudeUsageQuery\.data\}/)
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

test('Claude extra usage is a deliberate reversible account action', () => {
  assert.match(usageView, /Extra usage \$\{enabled \? 'enabled' : 'disabled'\}/)
  assert.match(usageView, /Enable' : 'Disable'\} paid extra usage\?/)
  assert.match(usageView, /onToggle\(next, enabled\)/)
  assert.match(settingsView, /api\.settings\.setClaudeExtraUsage\(enabled, expectedEnabled\)/)
})

test('Claude banked resets require a current provider offer and confirmation', () => {
  assert.match(usageView, /provider === 'claude'/)
  assert.match(usageView, /!resets\.redeemable/)
  assert.match(usageView, /resets\.nextCreditResetsLeft \|\| null/)
  assert.match(settingsView, /api\.settings\.redeemClaudeReset\(creditId, expectedResetsLeft\)/)
  assert.match(settingsView, /onRedeemClaudeReset=\{handleRedeemClaudeReset\}/)
})
