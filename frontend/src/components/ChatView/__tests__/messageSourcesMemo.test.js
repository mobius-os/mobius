/* Stable historical source inputs skip repeated extraction after pagination. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'

globalThis.window = { location: { origin: 'http://localhost', href: 'http://localhost/shell/' } }
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { sameMessageSourcesProps } = await vite.ssrLoadModule('/src/components/ChatView/MessageSources.jsx')
after(() => vite.close())

test('older-history prepend reuses references work for unchanged historical blocks', () => {
  const blocks = [{ type: 'tool', sources: [{ url: 'https://example.com' }] }]
  const ref = { message_index: 12, count: 1 }
  const before = { chatId: 'chat', disclosureKey: 'reply:references', groups: [blocks], refs: [ref] }
  assert.equal(sameMessageSourcesProps(before, { ...before, groups: [blocks], refs: [ref] }), true)
  assert.equal(sameMessageSourcesProps(before, { ...before, groups: [[...blocks]] }), false)
  assert.equal(sameMessageSourcesProps(before, { ...before, refs: [{ ...ref }] }), false)
  assert.equal(sameMessageSourcesProps(before, { ...before, disclosureKey: 'next:references' }), false)
  assert.equal(sameMessageSourcesProps(before, { ...before, chatId: 'other' }), false)
})
