import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { default: SharedBrowserAccess } = await vite.ssrLoadModule(
  '/src/components/SharedBrowserAccess/SharedBrowserAccess.jsx',
)

after(() => vite.close())

function render(initialEntry) {
  return renderToStaticMarkup(createElement(
    QueryClientProvider, { client: new QueryClient() },
    createElement(SharedBrowserAccess, { initialEntry }),
  ))
}

test('the leave confirmation asks first and leaves only by a same-origin POST', () => {
  const html = render({ kind: 'leave', value: 'grant/1' })
  assert.match(html, /<h1>Leave this shared Möbius\?<\/h1>/)
  assert.match(html, /<form action="[^"]*\/api\/connect\/browser-access\/session\/account\/leave\/start\?grant_id=grant%2F1" method="post">/)
  assert.match(html, /<button type="submit">Leave<\/button>/)
  assert.match(html, /data-mobius-visual-state="settled"/)
})

test('the page the sharing Möbius returns to after leaving says access ended', () => {
  const html = render({ kind: 'left', value: 'grant-1' })
  assert.match(html, /<h1>You left this shared Möbius<\/h1>/)
  assert.doesNotMatch(html, /<form/)
})
