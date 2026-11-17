/* Stable historical source inputs skip repeated extraction after pagination. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

globalThis.window = { location: { origin: 'http://localhost', href: 'http://localhost/shell/' } }
const vite = await createSsrRenderServer()
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

test('references comparison reads an omitted source input as none', () => {
  const bare = { chatId: 'chat', disclosureKey: 'reply:references' }
  assert.equal(sameMessageSourcesProps(bare, { ...bare }), true)
  assert.equal(sameMessageSourcesProps(bare, { ...bare, groups: [], refs: [] }), true)
  assert.equal(sameMessageSourcesProps(bare, { ...bare, groups: [[{ type: 'tool' }]] }), false)
})

test('an answer that continues after settling holds its References again', () => {
  // A question answered in its reply's row turns a settled answer back into a
  // continuing one with the same sources: held must still reach the section.
  const blocks = [{ type: 'tool', sources: [{ url: 'https://example.com' }] }]
  const settled = { chatId: 'chat', disclosureKey: 'reply:references', groups: [blocks], refs: [], held: false }
  assert.equal(sameMessageSourcesProps(settled, { ...settled, held: true }), false)
  assert.equal(sameMessageSourcesProps({ ...settled, held: true }, settled), false)
  assert.equal(sameMessageSourcesProps(settled, { ...settled, held: undefined }), true)
})
