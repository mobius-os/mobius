/* Gemini connects with an AI Studio or Agent Platform key; the row in use says Connected. */
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

test('not connected: both ways are offered, with why there is no subscription sign-in', () => {
  const html = text(render())
  assert.match(html, /AI Studio key Google AI Studio Connect/)
  assert.match(html, /Agent Platform key Google Cloud project · Gemini Enterprise Agent Platform Connect/)
  assert.match(html, /AI Pro or Ultra subscription sign-in isn’t allowed for third-party apps/)
  assert.match(html, /Google Cloud sign-in Sign in with Google · billed to your Cloud project \(Agent Platform\) Sign in/)
  assert.doesNotMatch(html, /Connected|Disconnect/)
})

test('connected: the row in use says so and the other offers a switch', () => {
  const html = text(render({ active: 'agent_platform' }))
  assert.match(html, /Agent Platform key Connected .* Reconnect Disconnect…/)
  assert.match(html, /AI Studio key Google AI Studio Use instead/)
  assert.match(html, /Google Cloud sign-in .* Use instead/)
  assert.doesNotMatch(html, /AI Studio key Connected/)
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

test('signed in to Google Cloud: that row is in use, both keys offer a switch', () => {
  const html = text(render({ active: 'google_cloud' }))
  assert.match(html, /Google Cloud sign-in Connected .* Sign in again Disconnect…/)
  assert.equal((html.match(/Use instead/g) || []).length, 2)
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
