/* A provider page lists its chat models with picker switches that reflect the owner's saved visibility. */
import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { default: ChatModelsList } = await vite.ssrLoadModule('/src/components/SettingsView/ChatModelsList.jsx')
const { modelQueries } = await vite.ssrLoadModule('/src/hooks/queries.js')
after(() => vite.close())

function render({ registry, hidden = [], configured = true, provider = 'claude' }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  try {
    client.setQueryData(modelQueries.registry.key, registry)
    client.setQueryData(modelQueries.prefs.key, { hidden_ids: hidden })
    return renderToStaticMarkup(createElement(QueryClientProvider, { client },
      createElement(ChatModelsList, { provider, configured })))
  } finally {
    client.clear()
  }
}

const registry = {
  claude: [{ id: 'claude-opus-x', label: 'Claude Opus X' }, { id: 'claude-haiku-x', label: 'Claude Haiku X' }],
  codex: [{ id: 'gpt-x', label: 'GPT-X' }],
}

test('each of this provider\'s models is listed with its picker switch state', () => {
  const html = render({ registry, hidden: ['claude-haiku-x'] })
  assert.match(html, /Claude Opus X[\s\S]*role="switch"[^>]*aria-checked="true"/)
  assert.match(html, /Claude Haiku X[\s\S]*role="switch"[^>]*aria-checked="false"/)
  assert.doesNotMatch(html, /GPT-X/)
})

test('switches are locked until the provider is connected', () => {
  const html = render({ registry, configured: false })
  const switches = html.match(/<button[^>]*role="switch"[^>]*>/g) || []
  assert.equal(switches.length, 2)
  for (const tag of switches) assert.match(tag, /disabled=""/)
})

test('a provider with no models says so instead of rendering an empty card', () => {
  const html = render({ registry: { claude: [] } })
  assert.match(html, /No chat models are available right now\./)
})
