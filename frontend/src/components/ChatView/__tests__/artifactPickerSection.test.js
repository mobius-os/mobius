import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import ArtifactPickerSection from '../ArtifactPickerSection.jsx'

test('artifact disclosure keeps its count with the heading and shows every app icon', () => {
  const html = renderToStaticMarkup(createElement(ArtifactPickerSection, {
    latestArtifact: {
      key: 'app:8',
      kind: 'app',
      title: 'Reflection',
      touchedAt: '2026-08-27T17:00:00Z',
      app: { id: 8, name: 'Reflection', icon_url: '/api/apps/8/icon?v=1' },
    },
    otherArtifacts: [{
      key: 'app:9',
      kind: 'app',
      title: 'Memory',
      touchedAt: '2026-08-27T16:00:00Z',
      app: { id: 9, name: 'Memory', icon_url: '/api/apps/9/icon?v=1' },
    }],
    expanded: true,
    onToggle: () => {},
    onOpenArtifact: () => {},
    disclosureIcon: createElement('svg', { 'aria-hidden': 'true' }),
  }))

  assert.match(
    html,
    /composer-popover__artifact-heading[^>]*>[\s\S]*?Latest artifacts[\s\S]*?composer-popover__artifact-count[^>]*>2</,
  )
  assert.match(html, /aria-label="Hide other artifacts"/)
  assert.match(html, /src="\/api\/apps\/8\/icon\?v=1&amp;size=128"/)
  assert.match(html, /src="\/api\/apps\/9\/icon\?v=1&amp;size=128"/)
  assert.match(html, />Memory</)
})
