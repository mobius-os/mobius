/* Gemini's Connection box: Sign in with Google or a key, then Claude-style Reconnect and Disconnect…. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GeminiConnection, { GEMINI_CONNECTIONS, geminiKeyRecord } from '../GeminiConnection.jsx'

function render(props = {}) {
  const client = new QueryClient()
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(GeminiConnection, props),
  ))
  client.clear()
  return html
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ')

test('not connected: Sign in with Google (Google Cloud) leads, keys fold under one row', () => {
  const html = text(render())
  assert.match(html, /Sign in with Google .*Google AI Pro and Ultra include monthly Cloud credits/)
  assert.match(html, /Connect with an API key instead AI Studio key or Agent Platform key/)
  assert.doesNotMatch(html, /Where should Gemini run|AI Studio API key|Save and connect/, 'no AI Studio sign-in; keys stay folded until opened')
  assert.doesNotMatch(html, /Connected|Disconnect|Reconnect/)
})

test('connected: Reconnect and Disconnect… sit together, other ways fold away', () => {
  const html = text(render({ active: 'agent_platform' }))
  assert.match(html, /Reconnect Disconnect… Other ways to connect/)
  assert.doesNotMatch(html, /Sign in with Google|API key/, 'the other ways stay folded until opened')
})

test('each key is kept from agents and allowed only on its own Gemini endpoints', () => {
  const [studio, platform] = GEMINI_CONNECTIONS
  assert.deepEqual(geminiKeyRecord(studio, 'k').grants, {
    agents: false,
    sites: [{ host: 'generativelanguage.googleapis.com', methods: ['GET', 'POST'], paths: ['/v1beta/models', '/v1beta/models/*'] }],
  })
  assert.deepEqual(geminiKeyRecord(platform, 'k').grants.sites, [{
    host: 'aiplatform.googleapis.com', methods: ['GET', 'POST'],
    paths: ['/v1/publishers/google/models', '/v1/publishers/google/models/*'],
  }])
  assert.deepEqual(geminiKeyRecord(platform, 'k').inject, { type: 'header', name: 'x-goog-api-key' })
  assert.deepEqual(GEMINI_CONNECTIONS.map(c => c.key), ['gemini', 'gemini-agent-platform'])
})

test('the Connection box names the way in use and, for a sign-in, its project', async () => {
  const { geminiConnectionDetails } = await import('../GeminiConnection.jsx')
  assert.deepEqual(geminiConnectionDetails({ authenticated: false, connection: 'ai_studio' }), [])
  assert.deepEqual(geminiConnectionDetails({ authenticated: true, connection: 'ai_studio' }), [
    { label: 'Connected with', value: 'AI Studio key' },
    { label: 'Key', value: 'Saved in Settings › Keys' },
  ])
  const rows = geminiConnectionDetails({
    authenticated: true, connection: 'google_cloud', project: { id: 'proj-123456', name: 'My First Project' },
  })
  assert.deepEqual(rows.map(row => row.label), ['Connected with', 'Project'])
  assert.equal(rows[0].value, 'Google Cloud sign-in')
  assert.match(text(renderToStaticMarkup(rows[1].value)), /My First Project proj-123456/)
})

test('Google Cloud sign-in: the button spins until Google’s link is ready, then opens it', async () => {
  const { default: GoogleCloudSignIn, SIGN_IN_BUTTON } = await import('../GoogleCloudSignIn.jsx')
  const client = new QueryClient()
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(GoogleCloudSignIn)))
  client.clear()
  assert.match(html, /<button[^>]*disabled=""[^>]*aria-busy="true"[^>]*><progress class="pa__spinner"[^>]*><\/progress>Sign in with Google<\/button>/)
  assert.deepEqual(SIGN_IN_BUTTON.ready, { label: 'Sign in with Google', busy: false })
  assert.deepEqual(SIGN_IN_BUTTON.failed, { label: 'Try again', busy: false })
})

test('Google Cloud sign-in: the sealed state rides back with the pasted code', async () => {
  const { signInState } = await import('../GoogleCloudSignIn.jsx')
  assert.equal(signInState('https://accounts.google.com/o/oauth2/auth?client_id=x&state=sealed-abc&scope=y'), 'sealed-abc')
  assert.equal(signInState('not a url'), '')
})
