/* Möbius model access (credit, trial, rates) on the Möbius provider page: strict parsing and a confirmed trial activation. */
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

function render(seed, props = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  try {
    for (const [key, data] of seed) client.setQueryData(key, data)
    return renderToStaticMarkup(createElement(QueryClientProvider, { client },
      createElement(MobiusModelAccess, props)))
  } finally {
    client.clear()
  }
}
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
