/* A branch shows where its copied history ends: "Branched from <chat>", whose
 * title opens the source while that chat still exists. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
after(() => vite.close())
const { default: BranchOrigin } = await vite.ssrLoadModule('/src/components/ChatView/BranchOrigin.jsx')

const origin = { chat_id: 'source', title: 'Trip plan', through_count: 2, available: true }

test('a branch names its source chat as a link that opens it', () => {
  const html = renderToStaticMarkup(createElement(BranchOrigin, { origin, onInternalNav: () => {} }))
  assert.match(html, /Branched from/)
  assert.match(html, /<button type="button" class="chat__branch-origin-link">Trip plan<\/button>/)
})

test('a deleted source is named but not offered as a link', () => {
  const html = renderToStaticMarkup(createElement(BranchOrigin, {
    origin: { ...origin, available: false }, onInternalNav: () => {},
  }))
  assert.match(html, /Trip plan/)
  assert.doesNotMatch(html, /<button/)
})
