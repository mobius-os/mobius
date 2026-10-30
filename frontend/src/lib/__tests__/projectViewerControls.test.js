/* Focused contracts for built artifact viewer controls and PDF sizing. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { default: ProjectPdfPreview, projectPdfScale } = await vite.ssrLoadModule('/src/components/Projects/ProjectPdfPreview.jsx')
const { default: ArtifactWorkspace } = await vite.ssrLoadModule('/src/components/Projects/ArtifactWorkspace.jsx')
const { projectQueries } = await vite.ssrLoadModule('/src/hooks/queries.js')
after(() => vite.close())

test('fit page respects both viewport dimensions; fit width caps upscaling before zoom', () => {
  const page = { width: 400, height: 800 }
  assert.equal(projectPdfScale(page, 1228, 628, 'page', 1), 0.75)
  assert.equal(projectPdfScale(page, 1228, 628, 'width', 1), 2)
  assert.equal(projectPdfScale(page, 1228, 628, 'width', 1.5), 3)
  assert.equal(projectPdfScale(page, 1228, 628, 'page', 2), 1.5)
})

test('PDF controls are opt-in for the built viewer, leaving source previews unchanged', () => {
  const render = props => renderToStaticMarkup(React.createElement(ProjectPdfPreview, { title: 'Paper', ...props }))
  assert.doesNotMatch(render({}), /Fit page|Zoom in|Download/)
  const built = render({ controls: true, defaultFit: 'page', onDownload() {} })
  assert.match(built, /aria-pressed="true"[^>]*>Fit page/)
  for (const label of ['Fit width', 'Zoom in', 'Zoom out', 'Download']) assert.ok(built.includes(label))
})

test('built viewer exposes project navigation and respects read-only rebuild permission', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(projectQueries.keys.artifacts('p'), [{
    id: 'paper', name: 'Paper', preview: 'pdf', status: 'ready', has_output: true,
    output_rel: 'artifacts/paper/output/paper.pdf',
  }])
  const render = readOnly => renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
    React.createElement(ArtifactWorkspace, { projectId: 'p', artifactId: 'paper', readOnly, onOpenProject() {} })))
  assert.match(render(false), /Back to project/)
  assert.match(render(false), />Rebuild</)
  assert.doesNotMatch(render(true), />Rebuild</)
})

test('only the owner shell offers a credential-free isolated website result tab', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(projectQueries.keys.artifacts('project-7'), [{
    id: 'site', name: 'Site', preview: 'html', status: 'ready', has_output: true,
    output_rel: 'artifacts/site/output/index.html',
  }])
  const render = allowOpenInNewTab => renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
    React.createElement(ArtifactWorkspace, { projectId: 'project-7', artifactId: 'site', allowOpenInNewTab })))
  const owner = render(true)
  assert.match(owner, /href="\/shell\/\?project=project-7&amp;result=site" target="_blank" rel="noopener noreferrer"/)
  assert.doesNotMatch(owner, /token=|artifacts\/site\/output/)
  assert.doesNotMatch(render(false), /Open in new tab/)
})
