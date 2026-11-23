import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { default: ChatSettingsPanel } = await vite.ssrLoadModule('/src/components/ChatView/ChatSettingsPanel.jsx')
const { authQueries, modelQueries } = await vite.ssrLoadModule('/src/hooks/queries.js')
after(() => vite.close())

const registry = {
  providers: {
    claude: [{ id: 'claude-x', label: 'Claude X' }],
    codex: [{ id: 'gpt-x', label: 'GPT-X' }],
  },
  defaults: {},
  replacements: {},
}
const statuses = {
  claude: { configured: true },
  codex: { configured: true },
}
const request = {
  chatId: 'chat-1',
  sourceProvider: 'claude',
  provider: 'codex',
  model: 'gpt-x',
  efforts: [],
  switchId: 'switch-1',
}

function render(providerSwitchState) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  try {
    client.setQueryData(modelQueries.registry.key, registry)
    client.setQueryData(modelQueries.prefs.key, { hidden_ids: [] })
    client.setQueryData(authQueries.provider.statuses.key, statuses)
    return renderToStaticMarkup(createElement(QueryClientProvider, { client },
      createElement(ChatSettingsPanel, {
        chatId: 'chat-1',
        provider: 'claude',
        effective: { model: 'claude-x', effort: 'high' },
        hasAssistantTurns: true,
        settingsSaveTailRef: { current: Promise.resolve() },
        providerSwitchState,
      })))
  } finally {
    client.clear()
  }
}

test('confirming a provider switch says the chat continues from its digest and recent messages', () => {
  const html = render({ status: 'confirming', request, error: '', result: null })
  assert.match(html, /OpenAI Codex continues from this chat&#x27;s saved digest and recent messages\./)
  assert.match(html, /title="Switch this chat to this provider"/)
  assert.match(html, />Switch provider</)
  assert.doesNotMatch(html, /prepare|Prepar|running summary/)
})

test('a switch in flight reads Switching and does not claim a slow preparation', () => {
  const html = render({ status: 'switching', request, error: '', result: null })
  assert.match(html, /<button[^>]*disabled=""[^>]*>Switching…</)
  assert.match(html, /Switching this chat to OpenAI Codex…/)
  assert.doesNotMatch(html, /prepare|Prepar/i)
})

test('a failed switch keeps the retryable confirmation with the server reason', () => {
  const html = render({ status: 'error', request, error: 'Could not switch this chat to the new provider.', result: null })
  assert.match(html, /role="alert"[^>]*>Could not switch this chat to the new provider\./)
  assert.match(html, />Switch provider</)
})

test('the switch failure fallbacks describe a switch, not a preparation', () => {
  // The fetch handler is not reachable from a static render, so pin its text.
  const source = readFileSync(new URL('../ChatSettingsPanel.jsx', import.meta.url), 'utf8')
  assert.match(source, /'Could not switch this chat to the new provider\.'/)
  assert.match(source, /'Network error while switching provider\.'/)
  assert.doesNotMatch(source, /Could not prepare|while preparing/)
})
