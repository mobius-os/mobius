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
const { default: ArtifactWorkspace } = await vite.ssrLoadModule('/src/components/Projects/ArtifactWorkspace.jsx')
const { projectQueries } = await vite.ssrLoadModule('/src/hooks/queries.js')
const { default: ProjectWorkspace } = await vite.ssrLoadModule(
  '/src/components/Projects/ProjectWorkspace.jsx',
)

after(() => vite.close())

function renderWorkspace(props = {}) {
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
        ...props,
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


test('the first build shows progress instead of requesting a nonexistent Creation', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(projectQueries.keys.artifacts('building-project'), [{
    id: 'game', name: 'Game', builder: 'game', preview: 'html', status: 'building', has_output: false,
  }])
  const markup = renderToStaticMarkup(React.createElement(QueryClientProvider, {client},
    React.createElement(ArtifactWorkspace, { projectId: 'building-project', artifactId: 'game' })))
  assert.match(markup, /Building your Creation/)
  assert.doesNotMatch(markup, /<iframe/)
  assert.match(markup, /disabled=""/)
})

test('a failed rebuild keeps the previous Creation and explains what is shown', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(projectQueries.keys.artifacts('failed-project'), [{
    id: 'game', name: 'Game', builder: 'game', preview: 'html', status: 'error', has_output: true,
  }])
  const markup = renderToStaticMarkup(React.createElement(QueryClientProvider, {client},
    React.createElement(ArtifactWorkspace, { projectId: 'failed-project', artifactId: 'game' })))
  assert.match(markup, /Showing the last successful Creation/)
  assert.doesNotMatch(markup, /Nothing built yet/)
})


test('linked app Projects open and explicitly build the real app without a duplicate Creation', () => {
  const markup = renderWorkspace({ linkedApp: { id: 123, name: 'Clock', source_dir: '/data/apps/clock' } })
  assert.match(markup, />Build &amp; update app<\/button>/)
  assert.match(markup, /Saved changes require Build &amp; update app/)
  assert.match(markup, /Save your files first/)
  assert.match(markup, />Open app<\/button>/)
  assert.doesNotMatch(markup, />Creations<\/h2>|draft preview/)
  assert.match(markup, /Inherited Möbius theme/)
  assert.match(markup, /class="project-source-notice"/)
  assert.doesNotMatch(renderWorkspace(), />Build &amp; update app<\/button>/)
})

test('managed View source advertises draft-only saves and an explicit Apply action', async () => {
  const { default: AppSourceWorkspace } = await vite.ssrLoadModule('/src/components/Projects/AppSourceWorkspace.jsx')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const app = { id: 123, name: 'Clock', source_dir: '/data/apps/clock' }
  const markup = renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
    React.createElement(AppSourceWorkspace, { app, requiresApply: true })))
  assert.match(markup, />Build &amp; update app<\/button>/)
  assert.match(markup, /Saved changes require Build &amp; update app/)
})


test('legacy imported app copies keep the real Project workspace without installed-app Apply', () => {
  const markup = renderWorkspace({ project: {
    id: 'legacy-app-copy', name: 'Preserved copy', chats: [],
    template: { imported_from: { kind: 'app', id: 123 } },
  } })
  assert.match(markup, /aria-label="Preserved copy project"/)
  assert.match(markup, /aria-label="Project overview"/)
  assert.match(markup, />Creations<\/h2>/)
  assert.match(markup, />Collaborate<\/span>/)
  assert.doesNotMatch(markup, /app-source-workspace|>Build &amp; update app<\/button>/)
})

test('unavailable linked apps keep source accessible without falling back to duplicate previews', () => {
  const markup = renderWorkspace({ project: {
    id: 'missing-app', name: 'Clock', chats: [],
    template: { imported_from: { kind: 'app', id: '12', management: 'linked' } },
  } })
  assert.match(markup, /linked app is unavailable/)
  assert.match(markup, /aria-label="File actions"/)
  assert.doesNotMatch(markup, />Creations<\/h2>|>Build &amp; update app<\/button>/)
})

test('old duplicate preview links lead to the installed app rather than an inert HTML copy', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(projectQueries.keys.artifacts('clock'), [])
  const project = { template: {
    imported_from: { kind: 'app', management: 'linked', id: '12' },
    retired_app_previews: ['app'],
  } }
  const markup = renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
    React.createElement(ArtifactWorkspace, { projectId: 'clock', project, artifactId: 'app', onOpenApp() {} })))
  assert.match(markup, />Open app<\/button>/)
  assert.doesNotMatch(markup, /<iframe/)
})
