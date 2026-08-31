import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  // Bundle the SDK UI dependency during SSR; its package intentionally uses
  // extensionless internal imports that native Node ESM does not resolve.
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: ProjectWorkspace } = await vite.ssrLoadModule(
  '/src/components/Projects/ProjectWorkspace.jsx',
)

after(() => vite.close())

function renderWorkspace() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const project = {
    id: 'project-1',
    name: 'Research notes',
    project_type: 'blank',
    chats: [],
  }
  return renderToStaticMarkup(
    React.createElement(
      QueryClientProvider,
      { client },
      React.createElement(ProjectWorkspace, {
        project,
        onCreateChat() {},
        onDelete() {},
        onOpenArtifact() {},
        onOpenChat() {},
        onRename() {},
      }),
    ),
  )
}

test('Creations, Chats, and Files form one ordered project workspace without tabs', () => {
  const markup = renderWorkspace()
  assert.doesNotMatch(markup, /role="tablist"|role="tab"|role="tabpanel"/)
  assert.match(markup, /aria-label="Project overview"/)
  const artifacts = markup.indexOf('>Creations</h2>')
  const chats = markup.indexOf('>Chats</h2>')
  const files = markup.indexOf('aria-label="Folder location"')
  assert.ok(artifacts >= 0 && artifacts < chats)
  assert.ok(chats < files)
})

test('the file explorer owns filtering and creation while the workspace has no redundant action header', () => {
  const markup = renderWorkspace()
  assert.match(markup, /class="project-finder__explorer"/)
  assert.match(markup, /aria-label="Research notes workspace"/)
  assert.match(markup, /role="toolbar" aria-label="File actions"/)
  for (const label of ['New file', 'New folder', 'Upload']) {
    assert.match(markup, new RegExp(`aria-label="${label}"`))
  }
  assert.match(markup, /placeholder="Filter files"/)
  assert.match(markup, /aria-label="New chat"/)
  assert.doesNotMatch(markup, /project-workspace__header/)
  assert.doesNotMatch(markup, /Actions for Research notes/)
  assert.doesNotMatch(markup, /project-build-button/)
})
