/* Server-render an element the way the shell does: inside a query client whose
   model registry already names the models tests use, as the picker loaded it. */
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

export const TEST_MODEL_REGISTRY = {
  claude: [{ id: 'claude-opus-4-8', label: 'Claude Opus 4.8' }],
  codex: [{ id: 'gpt-6-luna', label: 'GPT-6-Luna' }],
  mobius: [{ id: 'flow', label: 'Flow (GLM 5.3 Flash)' }],
}

export function renderWithModels(element, registry = TEST_MODEL_REGISTRY) {
  const client = new QueryClient()
  client.setQueryData(['models', 'registry'], registry)
  return renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, element))
}
