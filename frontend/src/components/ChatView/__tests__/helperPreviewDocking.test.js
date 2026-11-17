/* A chat pane shows one helper preview at a time, docked beside a wide chat or
   over a narrow one, and resizing across that width never redoes the dialog's
   focus handling. Only the preview's HTTP boundary and the pane's measured
   width are stubbed; no backend or browser layout is involved. */
import { after, afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { act } from 'react'
import { click, h, mount, unmountAll } from './mountedRender.js'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { TEST_MODEL_REGISTRY } from './modelRegistryRender.js'

const API_STUB = '\0docking-http'
const vite = await createSsrRenderServer({ plugins: [{
  name: 'docking-http', enforce: 'pre',
  resolveId: (source, importer) => importer?.endsWith('/HelperConversation.jsx') && source.endsWith('/api/client.js') ? API_STUB : null,
  load: id => id === API_STUB
    ? 'export const apiFetch = async () => ({ ok: true, status: 200, json: async () => ({ status: "completed", provider: "codex", blocks: [] }) })'
    : null,
}] })
after(() => vite.close())
const { HelperRow } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
const { default: WaitingChip } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
const { HelperPreviewProvider } = await vite.ssrLoadModule('/src/components/ChatView/helperPreviewSlot.jsx')
const client = new QueryClient()
client.setQueryData(['models', 'registry'], TEST_MODEL_REGISTRY)
after(() => client.clear())

// The pane's ResizeObserver, driven by hand.
let observers = []
const priorObserver = globalThis.ResizeObserver
beforeEach(() => {
  observers = []
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.push(this) }
    observe(target) { this.target = target }
    disconnect() { observers = observers.filter(observer => observer !== this) }
  }
})
afterEach(() => { unmountAll(); globalThis.ResizeObserver = priorObserver })

const helper = (id, name, status = 'completed') => ({ id, delegation_id: id, task_key: name, provider: 'codex', status })
const dialogs = () => document.body.querySelectorAll('[role="dialog"]')
const rowNamed = (root, name) => root.querySelectorAll('button').find(node => node.getAttribute('aria-label')?.startsWith(`${name}:`))
const closeButton = () => dialogs()[0].querySelectorAll('button').find(node => node.getAttribute('aria-label') === 'Close helper conversation')
const flush = () => act(async () => {})

function mountPane(width, { transcript = [], foot = [] } = {}) {
  const view = mount(h(HelperPreviewProvider, null, h('div', { className: 'chat' },
    h('div', { className: 'chat__scroll' },
      transcript.map(event => h(HelperRow, { key: event.id, event, chatId: 'parent' }))),
    h('div', { className: 'chat__foot' },
      h(WaitingChip, { chatId: 'parent', helpers: foot }),
      h('textarea', { 'aria-label': 'Message' })),
  )), { wrap: node => h(QueryClientProvider, { client }, node) })
  const pane = view.container.querySelector('.chat')
  pane.clientWidth = width
  const resize = async next => {
    pane.clientWidth = next
    await act(async () => { for (const observer of observers) if (observer.target === pane) observer.callback([]) })
  }
  return { view, pane, resize, composer: pane.querySelector('textarea') }
}

test('opening another helper in the same pane replaces its preview, whichever row opens it', async () => {
  const { pane } = mountPane(1200, {
    transcript: [helper('one', 'Survey'), helper('two', 'Draft')],
    foot: [helper('three', 'Check', 'running')],
  })
  click(rowNamed(pane, 'Survey'))
  await flush()
  assert.equal(dialogs().length, 1)
  assert.match(dialogs()[0].textContent, /Survey/)
  assert.ok(pane.querySelector('.helper-convo__overlay--docked'), 'a wide pane docks the preview, leaving its rows usable')

  click(rowNamed(pane, 'Draft'))
  await flush()
  assert.equal(dialogs().length, 1, 'a second transcript row replaces the preview instead of stacking another')
  assert.match(dialogs()[0].textContent, /Draft/)

  click(rowNamed(pane, 'Check'))
  await flush()
  assert.equal(dialogs().length, 1, 'the in-progress row takes the same single preview')
  assert.match(dialogs()[0].textContent, /Check/)

  click(rowNamed(pane, 'Survey'))
  await flush()
  assert.equal(dialogs().length, 1)
  assert.match(dialogs()[0].textContent, /Survey/, 'a row replaced earlier can open again')
  click(closeButton())
  assert.equal(dialogs().length, 0, 'closing the preview reveals no older one underneath')
})

test('a focused composer keeps its focus while a docked pane resizes, and crossing the width threshold never bounces focus to the opener', async () => {
  const { pane, resize, composer } = mountPane(1200, { transcript: [helper('one', 'Survey')] })
  const opener = rowNamed(pane, 'Survey')
  const openerFocus = opener.focus
  let openerFocused = 0
  opener.focus = function () { openerFocused += 1; return openerFocus.call(this) }
  opener.focus()
  openerFocused = 0
  click(opener)
  await flush()
  const dialog = dialogs()[0]
  assert.ok(document.activeElement === closeButton(), 'opening focuses the preview')
  assert.notEqual(composer.closest('.chat__foot').inert, true, 'a docked preview blocks nothing')

  composer.focus()
  await resize(1100)
  assert.ok(document.activeElement === composer, 'the reader keeps typing while the pane stays wide')

  await resize(900)
  assert.ok(dialogs()[0] === dialog, 'narrowing keeps the same dialog open')
  assert.ok(!pane.querySelector('.helper-convo__overlay--docked'), 'a narrow pane covers its chat')
  assert.equal(composer.closest('.chat__foot').inert, true, 'the covered chat is blocked')
  assert.ok(document.activeElement === closeButton(), 'focus the cover would strand moves into the preview')

  await resize(1200)
  assert.ok(dialogs()[0] === dialog)
  assert.notEqual(composer.closest('.chat__foot').inert, true, 'widening unblocks the chat again')
  assert.ok(document.activeElement === closeButton(), 'widening leaves focus where the reader has it')
  composer.focus()
  await resize(1000)
  await resize(1400)
  await flush()
  assert.ok(document.activeElement === composer, 'resizes that stay docked never pull focus from the composer')
  assert.equal(openerFocused, 0, 'no resize restores focus to the opener as if the dialog had closed')

  click(closeButton())
  assert.ok(document.activeElement === opener, 'closing still returns focus to the row that opened it')
})
