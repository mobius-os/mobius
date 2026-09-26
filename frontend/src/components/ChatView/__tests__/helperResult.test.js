/* A settled helper keeps its one row; its result is read in its conversation. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderWithModels } from './modelRegistryRender.js'
import { createServer } from 'vite'
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: Card } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
const priorWindow = globalThis.window
globalThis.window = { location: new URL('https://mobius.test/shell') }
after(() => { globalThis.window = priorWindow; return vite.close() })
function render(extra = {}) {
  return renderWithModels(React.createElement(Card, { chatId: 'chat', event: {
    id: 'delegation:one:running', type: 'helper_result', status: 'completed', task_key: 'Review',
    delegation_id: 'one', provider: 'claude', model: 'claude-opus-4-8', duration_ms: 98_000,
    consumption: 'incorporated', body: '<script>untrusted result</script>', ...extra,
  } }))
}

test('a settled helper is one quiet row with how it ended and how long it took', () => {
  const html = render()
  assert.match(html, /chat__helper-row/)
  assert.doesNotMatch(html, /chat__helper-working/)
  assert.match(html, /Opus 4.8 · Finished/)
  assert.match(html, /1m 38s/)
  assert.match(html, /chat__subagent-dot--done/)
  assert.match(html, /aria-label="Review: [^"]*Open its conversation"/)
})

test('a helper is tagged with the model name the picker shows, or its raw id when unnamed', () => {
  assert.match(render(), /Opus 4\.8 · Finished/)
  assert.doesNotMatch(render(), /claude-opus-4-8/)
  assert.match(render({ provider: 'mobius', model: 'flow' }), /Flow \(GLM 5\.3 Flash\) · Finished/)
  assert.match(render({ model: 'claude-opus-9' }), /Claude · claude-opus-9 · Finished/)
  assert.match(render({ provider: 'codex', model: null }), /Codex · Finished/)
})

test('the row never shows the result text or claims what the agent did with it', () => {
  const html = render()
  assert.doesNotMatch(html, /untrusted result|<script/)
  assert.doesNotMatch(html, /Incorporated|Delivery recorded|resume work/)
})

test('failed, reviewable and stopped helpers say how they ended', () => {
  assert.match(render({ status: 'failed' }), /· Failed/)
  assert.match(render({ status: 'needs_review' }), /· Needs review/)
  assert.match(render({ status: 'cancelled' }), /· Stopped/)
  assert.match(render({ status: 'failed' }), /chat__subagent-dot--failed/)
})
