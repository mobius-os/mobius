/* Gemini connects with an AI Studio key entered by the owner, never a subscription sign-in. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GeminiAuth from '../GeminiAuth.jsx'

function render(props) {
  const client = new QueryClient()
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(GeminiAuth, props),
  ))
  client.clear()
  return html
}

test('Gemini asks for an AI Studio key in a password field and explains why', () => {
  const html = render()
  assert.match(html, /AI Studio API key/)
  assert.match(html, /<input[^>]*type="password"[^>]*autoComplete="off"/i)
  assert.match(html, /not a Google\s+AI Pro or Ultra sign-in/)
  assert.match(html, /sent only to Google/)
})

test('connect stays disabled until a key is typed', () => {
  assert.match(render(), /<button type="submit" class="pa__btn" disabled="">Connect<\/button>/)
})

test('the key link opens Google AI Studio as a compact external button', () => {
  const [, attributes, contents] = render().match(/<a\b([^>]*)>(.*?)<\/a>/s)
  assert.match(attributes, /href="https:\/\/aistudio.google.com\/app\/apikey"/)
  assert.match(attributes, /class="pa__btn pa__btn--sm pa__external-link"/)
  assert.match(attributes, /rel="noopener noreferrer"/)
  assert.match(contents, /Open Google AI Studio/)
})

test('the saved key is kept from agents and allowed only on Gemini model endpoints', async () => {
  const { geminiKeyRecord } = await import('../GeminiAuth.jsx')
  assert.deepEqual(geminiKeyRecord('k'), {
    value: 'k',
    inject: { type: 'header', name: 'x-goog-api-key' },
    grants: {
      agents: false,
      sites: [{
        host: 'generativelanguage.googleapis.com',
        methods: ['GET', 'POST'],
        paths: ['/v1beta/models', '/v1beta/models/*'],
      }],
    },
  })
})
