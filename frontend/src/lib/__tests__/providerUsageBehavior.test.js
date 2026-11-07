import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

import {
  bankedResetCredits,
  claudeRedeemOutcomeMessage,
  claudeResetCredits,
  providerAllowance,
  providerAllowanceSummary,
  providerWeeklyAllowance,
} from '../../components/SettingsView/providerUsage.js'
import { providerExtraUsage, rowUsage, signInExpiryNotice } from '../../components/SettingsView/providerUsage.js'

// Settings and its provider page rendered with real React and a seeded cache.
const vite = await createSsrRenderServer()
const { default: SettingsView } = await vite.ssrLoadModule('/src/components/SettingsView/SettingsView.jsx')
const { default: ProviderPage } = await vite.ssrLoadModule('/src/components/SettingsView/ProviderPage.jsx')
after(() => vite.close())


test('plan providers follow typed weekly meaning, not display labels or other limits', () => {
  assert.deepEqual(providerAllowance('codex', {
    state: 'ready',
    windows: [
      { kind: 'other', label: 'Weekly', used_percent: 20 },
      { kind: 'weekly', label: 'Renamed allowance', used_percent: 58 },
      { kind: 'other', label: 'Extra usage', used_percent: 75 },
    ],
  }), {
    kind: 'weekly',
    label: 'Weekly usage',
    usedPercent: 58,
    expiresAt: null,
  })
  assert.deepEqual(providerAllowance('claude', { state: 'ready', windows: [] }), {
    kind: 'five_hour',
    label: '5-hour usage',
    usedPercent: null,
    expiresAt: null,
  })
})

test('the brain gauges the 5-hour window and lists weekly beside it', () => {
  const snapshot = {
    state: 'ready',
    windows: [
      { kind: 'five_hour', label: '5-hour', used_percent: 25 },
      { kind: 'weekly', label: 'Weekly', used_percent: 10 },
      { kind: 'other', label: 'Opus weekly', used_percent: 90 },
    ],
  }
  const gauge = providerAllowance('claude', snapshot)
  assert.deepEqual(gauge, {
    kind: 'five_hour',
    label: '5-hour usage',
    usedPercent: 25,
    expiresAt: null,
  })
  assert.equal(providerAllowanceSummary('claude', gauge), '25% 5-hour usage')
  const weekly = providerWeeklyAllowance('claude', snapshot)
  assert.equal(providerAllowanceSummary('claude', weekly), '10% weekly usage')
})

test('weekly is gauged only when no 5-hour window is reported', () => {
  const snapshot = {
    state: 'ready',
    windows: [{ kind: 'weekly', label: '7-day', used_percent: 40 }],
  }
  assert.equal(providerAllowance('codex', snapshot).label, '7-day usage')
  assert.equal(providerWeeklyAllowance('codex', snapshot), null)
  assert.equal(providerWeeklyAllowance('mobius', {
    state: 'ready',
    windows: [{ kind: 'weekly', used_percent: 40 }],
  }), null)
})

test('Codex names its 10,080-minute allowance as a 7-day window', () => {
  assert.deepEqual(providerAllowance('codex', {
    state: 'ready',
    windows: [
      { kind: 'weekly', label: '7-day', used_percent: 12 },
    ],
  }), {
    kind: 'weekly',
    label: '7-day usage',
    usedPercent: 12,
    expiresAt: null,
  })
})

test('Möbius follows typed API-credit usage instead of weekly windows', () => {
  assert.deepEqual(providerAllowance('mobius', {
    state: 'ready',
    windows: [
      { kind: 'weekly', used_percent: 80 },
      {
        kind: 'api_credits',
        used_percent: 0,
        remaining_percent: 99.98,
        expires_at: '2026-09-07T19:40:34.682998+00:00',
      },
    ],
  }), {
    kind: 'api_credits',
    label: 'API credits usage',
    usedPercent: 0,
    expiresAt: '2026-09-07T19:40:34.682998+00:00',
  })
  assert.deepEqual(providerAllowance('mobius', { state: 'unavailable' }), {
    kind: 'api_credits',
    label: 'API credits usage',
    usedPercent: null,
    expiresAt: null,
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

test('plan allowance copy identifies a recent fallback reading', () => {
  const allowance = providerAllowance('claude', {
    state: 'ready',
    stale: true,
    windows: [
      { kind: 'weekly', label: 'Weekly', used_percent: 24 },
    ],
  })

  assert.equal(allowance.stale, true)
  assert.equal(
    providerAllowanceSummary('claude', allowance),
    '24% weekly usage · last available',
  )
})

test('banked reset count preserves an explicit zero while rejecting missing data', () => {
  assert.deepEqual(bankedResetCredits({ reset_credits: { available_count: 0, credits: [] } }), {
    availableCount: 0,
    credits: [],
  })
  assert.deepEqual(bankedResetCredits({}), { availableCount: 0, credits: [] })
  assert.equal(bankedResetCredits(null), null)
})

test('Claude reset helper preserves the provider-selected redeemable grant', () => {
  assert.deepEqual(claudeResetCredits({
    reset_credits: {
      available_count: 2,
      credits: [{ id: 'grant-next', resets_left: 2 }],
      eligible: true,
      redeemable: true,
      next_credit_id: 'grant-next',
    },
  }), {
    availableCount: 2,
    credits: [{ id: 'grant-next', resets_left: 2 }],
    eligible: true,
    redeemable: true,
    nextCreditId: 'grant-next',
    nextCreditResetsLeft: 2,
  })
  assert.equal(claudeResetCredits({}), null)
  assert.deepEqual(claudeRedeemOutcomeMessage('not_limited'), {
    tone: 'info',
    text: 'Your limits are already clear — no reset was spent.',
  })
  assert.deepEqual(claudeRedeemOutcomeMessage('unknown'), {
    tone: 'info',
    text: 'Claude may have applied that reset. Möbius will reconcile it before another reset.',
  })
})


test('enabled paid extra usage reports its separate percentage', () => {
  assert.deepEqual(providerExtraUsage({
    extra_usage: { enabled: true, used_percent: 37.5 },
  }), {
    label: 'Extra usage',
    usedPercent: 37.5,
    summary: '37.5% extra usage used',
  })
  assert.equal(providerExtraUsage({
    extra_usage: { enabled: false, used_percent: 0 },
  }), null)
})


test('enabled extra usage keeps a missing percentage unknown', () => {
  assert.deepEqual(providerExtraUsage({
    extra_usage: { enabled: true, available: null, used_percent: null },
  }), {
    label: 'Extra usage',
    usedPercent: null,
    summary: 'Extra usage enabled',
  })
})

test('a sign-in warns a week before its earliest possible end, then asks for a reconnect, never says expired', () => {
  const now = new Date('2026-11-06T12:00:00Z')
  // Quiet while far out or unknown.
  assert.equal(signInExpiryNotice('2026-11-20T12:00:00Z', now), null)
  assert.equal(signInExpiryNotice(null, now), null)
  assert.equal(signInExpiryNotice('not a date', now), null)
  // Claude may end a sign-in up to three days before the reported end, so the
  // week of notice counts down to that earlier point.
  assert.equal(signInExpiryNotice('2026-11-16T12:00:00Z', now).label, 'Reconnect within 7 days')
  assert.equal(signInExpiryNotice('2026-11-17T12:00:01Z', now), null)
  assert.equal(signInExpiryNotice('2026-11-12T08:00:00Z', now).label, 'Reconnect within 3 days')
  assert.equal(signInExpiryNotice('2026-11-09T20:00:00Z', now).label, 'Reconnect within a day')
  assert.equal(signInExpiryNotice('2026-11-07T12:00:00Z', now).label, 'Reconnect today')
  // Past the reported end the current access token may still work.
  const passed = signInExpiryNotice('2026-11-05T12:00:00Z', now)
  assert.equal(passed.label, 'Reconnect now')
  assert.equal(passed.endsBy.toISOString(), '2026-11-05T12:00:00.000Z')
})

test('the passed reconnect date reads as passed, not as a date the sign-in may end by', () => {
  const now = new Date('2026-11-06T12:00:00Z')
  assert.match(signInExpiryNotice('2026-11-10T12:00:00Z', now).title, /^May end by /)
  const passed = signInExpiryNotice('2026-11-05T12:00:00Z', now).title
  assert.match(passed, /^Reported to end .*; it may stop at any time$/)
  assert.doesNotMatch(passed, /May end by/)
})

const DAY_MS = 86_400_000
const inDays = days => new Date(Date.now() + days * DAY_MS).toISOString()

// The Claude row of the Settings overview, rendered from a seeded provider status.
function claudeRow(claudeStatus) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  try {
    client.setQueryData(['settings'], { background_agents: [{ provider: 'claude', enabled: true }] })
    client.setQueryData(['auth', 'providers', 'status'], { claude: { name: 'Claude', configured: true, ...claudeStatus } })
    client.setQueryData(['models', 'registry'], { claude: [] })
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(SettingsView, { active: true })))
    const start = html.indexOf('aria-label="Claude Code provider settings"')
    assert.ok(start >= 0, 'the Claude row is rendered')
    return html.slice(start, html.indexOf('</button>', start))
  } finally {
    client.clear()
  }
}

test('the overview row asks for a reconnect in place of the background model once the sign-in nears its end', () => {
  // Earliest possible end is three days before the reported one.
  const near = claudeRow({ login_expires_at: inDays(5.5) })
  assert.match(near, /class="settings-provider-summary__notice">Reconnect within 3 days</)
  assert.doesNotMatch(near, /Background:/)
  assert.match(claudeRow({ login_expires_at: inDays(-1) }), /settings-provider-summary__notice">Reconnect now</)
  for (const status of [{ login_expires_at: inDays(30) }, {}]) {
    const quiet = claudeRow(status)
    assert.match(quiet, /Background:/)
    assert.doesNotMatch(quiet, /Reconnect/)
  }
  // A provider that is not connected has nothing to reconnect.
  assert.doesNotMatch(claudeRow({ configured: false, login_expires_at: inDays(-1) }), /Reconnect/)
})

function claudePage(props) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } })
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(ProviderPage, {
    provider: 'claude',
    info: { label: 'Claude' },
    models: [],
    configured: true,
    enabled: true,
    connection: createElement('span', null, 'connection panel'),
    connectionDetails: [{ label: 'Version', value: '2.1.0' }],
    ...props,
  })))
}

test('the provider page lists the sign-in notice first in its Connection section', () => {
  const now = new Date()
  const near = claudePage({ signInNotice: signInExpiryNotice(inDays(5.5), now) })
  assert.match(near, /Sign-in<\/span><\/span><span class="settings-item__value"><span class="settings-badge settings-badge--warning" title="May end by [^"]+">Reconnect within 3 days</)
  assert.ok(near.indexOf('Sign-in') < near.indexOf('Version'))
  assert.match(claudePage({ signInNotice: signInExpiryNotice(inDays(-1), now) }), /title="Reported to end [^"]+">Reconnect now</)
  const quiet = claudePage({ signInNotice: signInExpiryNotice(inDays(30), now) })
  assert.doesNotMatch(quiet, /Sign-in|Reconnect/)
  assert.match(quiet, /Version/)
})

test('a models request lands on the provider page\'s Chat models section', () => {
  const chatModelsRef = () => {}
  // The section the focus request scrolls to and focuses carries its anchor.
  const tree = ProviderPage({ provider: 'claude', info: { label: 'Claude' }, models: [], configured: true, chatModelsRef, chatModelsAttention: true })
  const chatModels = tree.props.children.find(child => child?.props?.['aria-labelledby'] === 'settings-claude-chat-models')
  assert.equal(chatModels.props.ref, chatModelsRef)
  assert.equal(chatModels.props.tabIndex, -1)
  assert.match(chatModels.props.className, /settings-setup-target/)
  assert.doesNotMatch(claudePage({ chatModelsAttention: false }), /settings-setup-target/)
})

test('a window whose reset has passed reads as unknown in the row and the composer meters', () => {
  const now = new Date('2026-11-07T12:00:00Z')
  const restored = {
    state: 'ready',
    windows: [
      { kind: 'five_hour', label: '5-hour', used_percent: 92, resets_at: '2026-11-07T10:00:00Z' },
      { kind: 'weekly', label: 'Weekly', used_percent: 64, resets_at: '2026-11-06T00:00:00Z' },
    ],
  }
  // Overview row: no stale percentage for either window.
  assert.equal(rowUsage(restored, now), null)
  // Composer: the gauge and the weekly line beside it are unknown.
  assert.equal(providerAllowance('claude', restored, now).usedPercent, null)
  assert.equal(providerAllowanceSummary('claude', providerAllowance('claude', restored, now)), '5-hour usage')
  assert.equal(providerWeeklyAllowance('claude', restored, now), null)

  // A window still running keeps its reading; a passed one never stands in for it.
  const mixed = {
    state: 'ready',
    windows: [
      { kind: 'five_hour', label: '5-hour', used_percent: 30, resets_at: '2026-11-07T14:00:00Z' },
      { kind: 'weekly', label: 'Weekly', used_percent: 64, resets_at: '2026-11-06T00:00:00Z' },
    ],
  }
  assert.equal(providerAllowance('claude', mixed, now).usedPercent, 30)
  assert.equal(providerWeeklyAllowance('claude', mixed, now), null)
  assert.equal(rowUsage(mixed, now).kind, 'five_hour')
  assert.equal(rowUsage({
    state: 'ready',
    windows: [{ kind: 'weekly', label: 'Weekly', used_percent: 64, resets_at: '2026-11-10T00:00:00Z' }],
  }, now).percent, 64)
})
