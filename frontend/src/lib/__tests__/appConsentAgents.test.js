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

test('a confined scope shows its label, what it grants, the app’s reason and its tools', () => {
  const html = render({
    id: 'agent_role', level: 'Edit', critical: false, declared: true,
    grant: 'Can read and change this app’s files and data.',
    text: 'Builds the sites you make here.',
    tools: [
      { id: 'notify', title: 'Notifications', text: 'Send you notifications.', warning: false },
      { id: 'web', title: 'Web access', text: 'Search the web and open public pages.', warning: true },
    ],
  })
  assert.match(html, /app-consent__chip">Edit</)
  assert.match(html, /app-consent__agents-grant">Can read and change this app’s files and data\.</)
  assert.match(html, /App’s reason<\/p><p class="app-consent__agents-reason">Builds the sites you make here\./)
  assert.match(html, /Tools requested/)
  assert.match(html, /app-consent__tool--warning"><strong>Web access<\/strong><span>Search the web and open public pages\. App data may reach the web\./)
  assert.doesNotMatch(html, /Full access to your Möbius/)
})

test('a Tools scope without tools lists none, and an update names the scope it replaces', () => {
  const html = render({
    id: 'agent_role', level: 'Tools', critical: false, declared: true,
    grant: 'No access to this app’s data or the rest of your Möbius.',
    text: 'Looks up public sources.', note: 'Previously Read.', tools: [],
  })
  assert.match(html, /app-consent__chip">Tools</)
  assert.match(html, /app-consent__agents-note">Previously Read\.</)
  assert.doesNotMatch(html, /Tools requested/)
})

test('Admin leads with the fixed full-access callout', () => {
  const html = render({
    id: 'agent_start', level: 'Admin', critical: true, declared: true,
    grant: 'Can read and change every app, your data and your settings without asking.',
    text: 'Manages your apps.', tools: [],
  })
  assert.match(html, /app-consent__agents app-consent__agents--admin/)
  assert.match(html, /app-consent__chip app-consent__chip--critical">Admin</)
  assert.match(html, /role="note"[^>]*>.*<strong>Full access to your Möbius<\/strong>/s)
  assert.doesNotMatch(html, /Tools requested/)
})

test('an app that declares nothing shows the platform’s note, not an app reason', () => {
  const html = render({
    id: 'agent_start', level: 'Admin', critical: true, declared: false,
    grant: 'Can read and change every app, your data and your settings without asking.',
    text: 'Can open agent chats on its own.', tools: [],
  })
  assert.match(html, /Full access to your Möbius/)
  assert.doesNotMatch(html, /App’s reason/)
  assert.match(html, /Can open agent chats on its own\./)
})
