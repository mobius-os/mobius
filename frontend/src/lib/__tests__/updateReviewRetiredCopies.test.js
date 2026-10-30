/** Render the update review's list of local copies an update replaces. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { RetiredLocalCopies } = await vite.ssrLoadModule('/src/components/SettingsView/UpdateReviewModal.jsx')
after(() => vite.close())

test('each replaced local copy names its path, links its merged PR, and says the old copy stays recoverable', () => {
  const html = renderToStaticMarkup(createElement(RetiredLocalCopies, { copies: [
    { path: 'backend/app/a.py', pr: 1895, url: 'https://github.com/mobius-os/mobius/pull/1895' },
  ] }))
  assert.match(html, /<h3>Local copies that will be replaced<\/h3>/)
  assert.match(html, /<code>backend\/app\/a\.py<\/code>/)
  assert.match(html, /<a href="https:\/\/github\.com\/mobius-os\/mobius\/pull\/1895" target="_blank" rel="noopener noreferrer">#1895<\/a>/)
  assert.match(html, /Your old copies stay in the server’s history, so they can be recovered\./)
})

test('an update that replaces no local copies shows no section', () => {
  assert.equal(renderToStaticMarkup(createElement(RetiredLocalCopies, { copies: [] })), '')
})
