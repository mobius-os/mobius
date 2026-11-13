import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { default: AppConsentAgents } = await vite.ssrLoadModule(
  '/src/components/AppCanvas/AppConsentAgents.jsx',
)
after(() => vite.close())

const render = row => renderToStaticMarkup(createElement(AppConsentAgents, { row }))

test('a confined role shows its chip, the app’s own reason and its agent tools', () => {
  const html = render({
    id: 'agent_role', level: 'Editor', critical: false, text: 'Builds the sites you make here.',
    tools: [
      { id: 'notify', title: 'Notifications', text: 'Send you notifications.', warning: false },
      { id: 'web', title: 'Web access', text: 'Search the web and open public pages.', warning: true },
    ],
  })
  assert.match(html, /app-consent__chip">Editor</)
  assert.match(html, /Builds the sites you make here\./)
  assert.match(html, /Agent tools/)
  assert.match(html, /app-consent__tool--warning"><strong>Web access<\/strong><span>Search the web and open public pages\. App data may reach the web\./)
  assert.doesNotMatch(html, /Full access to your Möbius/)
  assert.doesNotMatch(html, /app-consent__dot/)
})

test('an owner role adds the fixed full-access line and the critical dot, with no tools list', () => {
  const html = render({
    id: 'agent_start', level: 'Owner', critical: true, text: 'Manages your apps.', tools: [],
  })
  assert.match(html, /app-consent__chip app-consent__chip--critical">Owner</)
  assert.match(html, /Full access to your Möbius\./)
  assert.match(html, /app-consent__dot/)
  assert.doesNotMatch(html, /Agent tools/)
})
