/* Rendered account guidance stays a short, ordered checklist with optional privacy advice. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CodexAuth from '../CodexAuth.jsx'

function render(props) {
  const client = new QueryClient()
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(CodexAuth, props),
  ))
  client.clear()
  return html
}

test('Codex setup explains each account action in its own ordered step', () => {
  const html = render()
  const list = html.match(/<ol[^>]*>(.*?)<\/ol>/s)?.[1]
  assert.ok(list, 'setup actions must be an ordered list')
  const steps = [...list.matchAll(/<li>(.*?)<\/li>/gs)]
    .map(([, item]) => item.replace(/<[^>]+>/g, ''))
  assert.deepEqual(steps, [
    'Open Settings → Security.',
    'Scroll to the very bottom.',
    'Turn on Enable device code authorization for Codex.',
    '(Optional for privacy) Open Settings → Data Controls and turn off Improve the model for everyone.',
  ])
  assert.match(html, /href="https:\/\/chatgpt.com\/#settings\/Security"/)
  assert.doesNotMatch(html, /help.openai.com|data-controls guide/)
  assert.match(html, />Get sign-in code</)
})

test('a caller can omit account setup without losing the sign-in action', () => {
  const html = render({ showSetupHint: false })
  assert.doesNotMatch(html, /<ol|Settings →|data-controls guide/)
  assert.match(html, />Get sign-in code</)
})

test('setup offers one button-styled settings link with an external-link indicator', () => {
  const links = [...render().matchAll(/<a\b([^>]*)>(.*?)<\/a>/gs)]
  assert.equal(links.length, 1)
  const [, attributes, contents] = links[0]
  assert.match(attributes, /class="pa__btn pa__btn--sm codex-auth__settings-button"/)
  assert.match(attributes, /target="_blank"/)
  assert.match(attributes, /rel="noopener noreferrer"/)
  assert.match(contents, /<svg[^>]*aria-hidden="true"/)
  assert.match(contents, /Open ChatGPT settings/)
})
