import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

// Render the credits sheets and the Settings list with real React.
const vite = await createSsrRenderServer()
const { AppCreditsPaymentDialog, AppCreditsApprovalDialog } =
  await vite.ssrLoadModule('/src/components/AppCanvas/AppCreditsDialogs.jsx')
const { default: CreditsAppsSection } =
  await vite.ssrLoadModule('/src/components/SettingsView/CreditsAppsSection.jsx')
after(() => vite.close())

const PAYMENT = {
  id: 'p1', app_name: 'Market', seller: 'ana', app: 'prompts', action: 'run',
  description: 'Run: Meeting notes', method: 'credits',
  price_micro: 400_000, price: '0.40', max_price_micro: 500_000, max_price: '0.50',
}

function render(element, seed = () => {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false } } })
  seed(client)
  try {
    return renderToStaticMarkup(createElement(QueryClientProvider, { client }, element))
  } finally {
    client.clear()
  }
}

function button(html, label) {
  const match = new RegExp(`<button[^>]*>${label}</button>`).exec(html)
  assert.ok(match, `no ${label} button in ${html}`)
  return match[0]
}

const noop = () => {}

test('Pay is off when the payment sheet first appears', () => {
  const html = render(createElement(AppCreditsPaymentDialog, {
    appId: 1, appName: 'Market', payment: PAYMENT, busy: false, error: '',
    onCancel: noop, onPay: noop, onClose: noop,
  }))
  assert.match(button(html, 'Pay 0.40 credits'), /disabled/)
  assert.doesNotMatch(button(html, 'Cancel'), /disabled/)
  assert.match(html, /By @ana · in Market/)
  assert.match(html, /pays at most 0.50 credits/)
})

test('a refused payment keeps its sheet with the reason and only a Close', () => {
  const html = render(createElement(AppCreditsPaymentDialog, {
    appId: 1, appName: 'Market', payment: { ...PAYMENT, method: 'card', currency: 'gbp' },
    busy: false, error: '', refusal: 'Card payments start at £0.50.',
    onCancel: noop, onPay: noop, onClose: noop,
  }))
  assert.match(html, /role="alert">Card payments start at £0.50.</)
  assert.match(html, /didn’t go through/)
  button(html, 'Close')
  assert.doesNotMatch(html, />Pay /)
  assert.match(html, /£0.40/)
  assert.doesNotMatch(html, /\$/)
})

test('Allow is off when a local app first asks about credits', () => {
  const html = render(createElement(AppCreditsApprovalDialog, {
    appId: 1, appName: 'Market', busy: false, error: '', onNotNow: noop, onAllow: noop,
    request: { app_name: 'Market', rows: [{ id: 'credits_manage', title: 'Manage your credits', text: 'See and move them.' }] },
  }))
  assert.match(button(html, 'Allow'), /disabled/)
  assert.match(html, /Manage your credits/)
})

test('Settings lists the local apps allowed to manage credits, each removable', () => {
  const html = render(createElement(CreditsAppsSection), client => {
    client.setQueryData(['credits-apps'], { apps: [
      { app_id: 7, name: 'Ledger', slug: 'ledger', rows: [{ id: 'credits_manage', title: 'Manage your credits' }] },
    ] })
  })
  assert.match(html, /Apps that can use your credits/)
  assert.match(html, /asked before it first managed your credits/)
  assert.match(html, /Ledger/)
  button(html, 'Remove')
})

test('Settings shows nothing when no local app manages credits', () => {
  const html = render(createElement(CreditsAppsSection), client => {
    client.setQueryData(['credits-apps'], { apps: [] })
  })
  assert.equal(html, '')
})
