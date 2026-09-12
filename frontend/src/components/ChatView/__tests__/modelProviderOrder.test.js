import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createServer } from 'vite'

const vite = await createServer({
  configFile: false,
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const {
  PROVIDER_INFO,
  PROVIDER_ORDER,
} = await vite.ssrLoadModule('/src/components/ChatView/ChatSettingsPanel.jsx')
const { default: ManageModelsModal } = await vite.ssrLoadModule(
  '/src/components/ChatView/ManageModelsModal.jsx',
)
const { modelQueries } = await vite.ssrLoadModule('/src/hooks/queries.js')

after(() => vite.close())

function renderModelManager() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  client.setQueryData(modelQueries.keys.registry, {
    codex: [{ id: 'gpt-5.6', label: 'GPT-5.6' }],
    claude: [{ id: 'claude-opus-4-7', label: 'Claude Opus 4.7' }],
    mobius: [{ id: 'mobius-max', label: 'Möbius Max' }],
  })
  client.setQueryData(modelQueries.keys.prefs, { hidden_ids: [] })

  return renderToStaticMarkup(
    React.createElement(
      QueryClientProvider,
      { client },
      React.createElement(ManageModelsModal, {
        onClose() {},
        providerOrder: PROVIDER_ORDER,
        providerInfo: PROVIDER_INFO,
        configuredProviders: new Set(PROVIDER_ORDER),
      }),
    ),
  )
}

test('chat model surfaces expose the connected providers before Möbius', () => {
  assert.deepEqual(PROVIDER_ORDER, ['codex', 'claude', 'mobius'])
  assert.equal(PROVIDER_INFO.mobius.label, 'Möbius subscription')

  const markup = renderModelManager()
  const codexAt = markup.indexOf('OpenAI Codex')
  const claudeAt = markup.indexOf('Claude Code')
  const mobiusAt = markup.indexOf('Möbius subscription')
  assert.ok(codexAt >= 0 && claudeAt > codexAt && mobiusAt > claudeAt)
})

test('Möbius exposes the provider mark through its rendered metadata', () => {
  const markup = renderToStaticMarkup(
    React.createElement(PROVIDER_INFO.mobius.Logo),
  )
  assert.equal(
    markup,
    '<span class="csp__mobius-logo" aria-hidden="true"></span>',
  )
})

test('public Möbius rows hide internal wire ids', () => {
  const markup = renderModelManager()
  assert.match(markup, />Möbius Max</)
  assert.doesNotMatch(markup, />mobius-max</)
  assert.match(markup, />gpt-5\.6</)
  assert.match(markup, />claude-opus-4-7</)
})

for (const [name, query, path, body, expected] of [
  ['registry', modelQueries.registry, '/api/models',
    { providers: { codex: [{ id: 'test-model' }] } }, { codex: [{ id: 'test-model' }] }],
  ['preferences', modelQueries.prefs, '/api/owner/model-prefs',
    { hidden_ids: ['test-model'] }, { hidden_ids: ['test-model'] }],
]) {
  test(`real ${name} read recovers from transient failures`, async () => {
    const originalFetch = globalThis.fetch
    const client = new QueryClient()
    let attempts = 0
    globalThis.fetch = async url => {
      assert.equal(new URL(url, 'https://example.test').pathname, path)
      attempts += 1
      return Response.json(attempts < 4 ? { detail: 'unavailable' } : body,
        { status: attempts < 4 ? 503 : 200 })
    }
    try {
      assert.deepEqual(await client.fetchQuery({ ...query.options(), retryDelay: 0 }), expected)
      assert.equal(attempts, 4)
    } finally {
      client.clear()
      globalThis.fetch = originalFetch
    }
  })
}

test('real preference query degrades only after four failed reads', async () => {
  const originalFetch = globalThis.fetch
  const client = new QueryClient()
  let attempts = 0
  globalThis.fetch = async url => {
    assert.equal(new URL(url, 'https://example.test').pathname, '/api/owner/model-prefs')
    attempts += 1
    return Response.json({ detail: 'unavailable' }, { status: 503 })
  }
  try {
    assert.deepEqual(await client.fetchQuery(modelQueries.prefs.options()), { hidden_ids: [] })
    assert.equal(attempts, 4, 'query retry must not multiply the read budget')
  } finally {
    client.clear()
    globalThis.fetch = originalFetch
  }
})
