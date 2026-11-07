/* Möbius model access on Settings: strict parsing, a confirmed trial activation, an honest provider state, and account reads only for the owner. */
import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { parseAgentAccess } from '../mobiusAccount.js'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

// Render the real section with real React and a seeded account cache.
const vite = await createSsrRenderServer()
const { MobiusModelAccess } = await vite.ssrLoadModule('/src/components/SettingsView/MobiusAccountPage.jsx')
const { mobiusAccountQueries } = await vite.ssrLoadModule('/src/hooks/mobiusAccountQueries.js')
const { settingsQueries } = await vite.ssrLoadModule('/src/hooks/queries.js')
const { beginSharedBrowserAuth } = await vite.ssrLoadModule('/src/api/client.js')
const { default: SettingsView } = await vite.ssrLoadModule('/src/components/SettingsView/SettingsView.jsx')
after(() => vite.close())
const signedOut = { agent_access: 'signed_out', models: [], balance: {}, trial: {}, retention: {} }
const available = {
  agent_access: 'available',
  models: [{ id: 'evolve', name: 'Evolve', pricing: { input: 1, cached_input: 0.1, output: 4 } }],
  balance: { available_units: 5_000_000 },
  trial: { state: 'active' },
  retention: { policy: 'local-testing-v1', notice: 'Requests are retained briefly for local testing.' },
}

test('signed-out model access may not carry prices, balance or trial data', () => {
  assert.deepEqual(parseAgentAccess(signedOut), signedOut)
  assert.throws(() => parseAgentAccess({ ...signedOut, models: available.models }), /without account access/)
  assert.throws(() => parseAgentAccess({ ...signedOut, balance: { available_units: 1 } }), /without account access/)
  assert.throws(() => parseAgentAccess({ ...signedOut, trial: { state: 'ready' } }), /without account access/)
})

test('unknown fields added by the identity service are ignored at every level', () => {
  const extended = {
    ...available,
    region: 'eu',
    models: [{ ...available.models[0], context_window: 200000, tier: 'pro', pricing: { ...available.models[0].pricing, batch: 0.5 } }],
    balance: { ...available.balance, available_usd: '5.00', currency: 'USD' },
    trial: { ...available.trial, expires_at: '2030-01-01T00:00:00Z' },
    retention: { ...available.retention, policy: 'a-new-policy', days: 30 },
  }
  assert.deepEqual(parseAgentAccess(extended), {
    agent_access: 'available',
    models: [{ id: 'evolve', name: 'Evolve', pricing: { input: 1, cached_input: 0.1, output: 4 } }],
    balance: { available_units: 5_000_000 },
    trial: { state: 'active' },
    retention: { notice: available.retention.notice },
  })
  assert.deepEqual(parseAgentAccess({ ...signedOut, hint: 'sign in' }), signedOut)
})

test('fields the page renders are still validated', () => {
  assert.throws(() => parseAgentAccess({ ...available, agent_access: 'maybe' }), /invalid model access/)
  assert.throws(() => parseAgentAccess({ ...available, models: [{ id: 'evolve', name: 'Evolve', pricing: { input: -1, cached_input: 0, output: 0 } }] }), /invalid model prices/)
  assert.throws(() => parseAgentAccess({ ...available, models: [available.models[0], available.models[0]] }), /duplicate/)
  assert.throws(() => parseAgentAccess({ ...available, balance: { available_units: 1.5 } }), /invalid model balance/)
  assert.throws(() => parseAgentAccess({ ...available, trial: {} }), /invalid trial state/)
  assert.throws(() => parseAgentAccess({ ...available, retention: { policy: 'x' } }), /retention/)
})

function render(seed, props = {}, component = MobiusModelAccess) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  try {
    for (const [key, data] of seed) client.setQueryData(key, data)
    return renderToStaticMarkup(createElement(QueryClientProvider, { client },
      createElement(component, props)))
  } finally {
    client.clear()
  }
}

// Whether each account read would start on mount ('fetching') or never runs ('idle').
function AccountReads() {
  const reads = {
    identity: mobiusAccountQueries.identity.useQuery(),
    agent: mobiusAccountQueries.agent.useQuery(),
    visibility: mobiusAccountQueries.visibility.useQuery(),
    railway: mobiusAccountQueries.railway.useQuery(),
    // The Möbius credit meter Settings and the composer show.
    mobiusUsage: settingsQueries.providerUsage.useQuery('mobius'),
    claudeUsage: settingsQueries.providerUsage.useQuery('claude'),
  }
  return JSON.stringify(Object.fromEntries(Object.entries(reads).map(([name, read]) => [name, read.fetchStatus])))
}
const accountReads = () => JSON.parse(render([], {}, AccountReads).replaceAll('&quot;', '"'))
const linked = { account_mode: 'linked', account_unavailable: false, profile: { handle: 'owner' }, deployments: [] }

test('a ready trial is offered behind a confirmation, never activated on sight', () => {
  const html = render([
    [mobiusAccountQueries.identity.key, linked],
    [mobiusAccountQueries.agent.key, { ...available, trial: { state: 'ready' }, balance: { available_units: 0 } }],
    [mobiusAccountQueries.visibility.key, true],
  ])
  assert.match(html, />Activate…<\/button>/)
  assert.doesNotMatch(html, />Activate trial</)
})

test('signed-out model access offers sign-in through the account page', () => {
  const html = render([
    [mobiusAccountQueries.identity.key, { account_mode: 'signed_out', account_unavailable: false, profile: null, deployments: [] }],
    [mobiusAccountQueries.visibility.key, true],
  ], { onSignIn: () => {} })
  assert.match(html, />Sign in<\/button>/)
  assert.match(html, /Show Möbius models in pickers/)
  assert.doesNotMatch(html, /Möbius · You/)
})

test('used-up credit is called out instead of showing a zero balance', () => {
  const html = render([
    [mobiusAccountQueries.identity.key, linked],
    [mobiusAccountQueries.agent.key, { ...available, trial: { state: 'expired' }, balance: { available_units: 0 } }],
    [mobiusAccountQueries.visibility.key, true],
  ])
  assert.match(html, /No credit/)
})

test('while the account is still loading the section waits quietly instead of asking to sign in', () => {
  const html = render([[mobiusAccountQueries.visibility.key, true]], { onSignIn: () => {} })
  assert.match(html, /aria-busy="true"/)
  assert.doesNotMatch(html, /Sign in/)
})

// The Möbius row of Settings › Background agents for one provider status.
function mobiusProviderRow(mobiusStatus) {
  const html = render([
    [['settings'], { background_agents: [{ provider: 'mobius', enabled: true }] }],
    [['auth', 'providers', 'status'], { mobius: { name: 'Möbius', ...mobiusStatus } }],
    [['models', 'registry'], { mobius: [] }],
  ], {}, SettingsView)
  const row = html.split('class="settings-bg-row ').find(part => part.includes('Möbius provider settings'))
  assert.ok(row, 'the Möbius row is rendered')
  return row.slice(0, row.indexOf('</button>'))
}

test('Möbius models turned off read as turned off, not as a signed-out account', () => {
  const off = mobiusProviderRow({ configured: false, available: false, error: 'Möbius models are turned off in Settings.' })
  assert.match(off, /Turned off/)
  assert.doesNotMatch(off, /Not signed in|Sign in/)
  const signedOut = mobiusProviderRow({ configured: false, available: true, error: 'Sign in to your Möbius account in Settings to use Möbius models.' })
  assert.match(signedOut, /Not signed in/)
  assert.match(signedOut, />Sign in</)
})

// Keep last: a shared-browser session cannot be ended within this process.
test('a shared-browser guest never reads the owner’s Möbius account or credit', () => {
  assert.deepEqual(accountReads(), {
    identity: 'fetching', agent: 'fetching', visibility: 'fetching', railway: 'fetching', mobiusUsage: 'fetching', claudeUsage: 'fetching',
  })
  beginSharedBrowserAuth()
  // A guest's own provider usage still loads; the owner's Möbius credit never does.
  assert.deepEqual(accountReads(), {
    identity: 'idle', agent: 'idle', visibility: 'idle', railway: 'idle', mobiusUsage: 'idle', claudeUsage: 'fetching',
  })
})
