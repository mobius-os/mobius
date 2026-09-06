/* Connection management does not begin sign-in merely by opening its panel. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ProviderConnection from '../ProviderConnection.jsx'

function render(connected) {
  const queryClient = new QueryClient()
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient },
    createElement(ProviderConnection, { provider: 'codex', name: 'OpenAI Codex', connected },
      createElement('div', null, 'Sign-in flow'),
    ),
  ))
  queryClient.clear()
  return html
}

test('connected provider offers both actions without mounting a sign-in flow', () => {
  const html = render(true)
  assert.match(html, />Reconnect</)
  assert.match(html, />Disconnect…</)
  assert.doesNotMatch(html, /Sign-in flow/)
  assert.doesNotMatch(html, />Disconnect</)
})

test('disconnected provider keeps its existing sign-in flow', () => {
  const html = render(false)
  assert.match(html, /Sign-in flow/)
  assert.doesNotMatch(html, /Reconnect|Disconnect/)
})
