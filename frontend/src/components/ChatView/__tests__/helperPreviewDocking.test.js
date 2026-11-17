/* A chat pane shows one helper preview at a time, docked beside a wide chat or
   over a narrow one, and resizing across that width never redoes the dialog's
   focus handling. Only the preview's HTTP boundary and the pane's measured
   width are stubbed; no backend or browser layout is involved. */
import { after, afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { act, useRef, useState } from 'react'
import { click, h, mount, unmountAll } from './mountedRender.js'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { TEST_MODEL_REGISTRY } from './modelRegistryRender.js'

const API_STUB = '\0docking-http'
const vite = await createSsrRenderServer({ plugins: [{
  name: 'docking-http', enforce: 'pre',
  resolveId: (source, importer) => importer?.endsWith('/HelperConversation.jsx') && source.endsWith('/api/client.js') ? API_STUB : null,
  load: id => id === API_STUB
    ? 'export const apiFetch = async (...args) => globalThis.__dockingRead ? globalThis.__dockingRead(...args) : ({ ok: true, status: 200, json: async () => ({ status: "completed", provider: "codex", blocks: [] }) })'
    : null,
}] })
after(() => vite.close())
const { HelperRow } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
const { default: SubagentChips } = await vite.ssrLoadModule('/src/components/ChatView/SubagentChips.jsx')
const { default: WaitingChip } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
const { HelperPreviewProvider } = await vite.ssrLoadModule('/src/components/ChatView/helperPreviewSlot.jsx')
const { default: useDialogFocus } = await vite.ssrLoadModule('/src/hooks/useDialogFocus.js')
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
afterEach(() => { unmountAll(); globalThis.ResizeObserver = priorObserver; delete globalThis.__dockingRead })

const helper = (id, name, status = 'completed') => ({ id, delegation_id: id, task_key: name, provider: 'codex', status })
const dialogs = () => document.body.querySelectorAll('[role="dialog"]')
const rowNamed = (root, name) => root.querySelectorAll('button').find(node => node.getAttribute('aria-label')?.startsWith(`${name}:`))
const closeButton = () => dialogs()[0].querySelectorAll('button').find(node => node.getAttribute('aria-label') === 'Close helper conversation')
const flush = () => act(async () => {})

function mountPane(width, { transcript = [], foot = [], subagent = null, panel = null } = {}) {
  const view = mount(h(HelperPreviewProvider, null, h('div', { className: 'chat' },
    panel,
    h('div', { className: 'chat__scroll' },
      transcript.map(event => h(HelperRow, { key: event.id, event, chatId: 'parent' })),
      subagent && h(SubagentChips, { subagent, chatId: 'parent' })),
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

test('closing a preview that replaced another returns focus to the row that opened it', async () => {
  const { pane } = mountPane(1200, { transcript: [helper('one', 'Survey'), helper('two', 'Draft')] })
  const survey = rowNamed(pane, 'Survey')
  const draft = rowNamed(pane, 'Draft')
  survey.focus()
  click(survey)
  await flush()
  draft.focus()
  click(draft)
  await flush()
  assert.match(dialogs()[0].textContent, /Draft/)
  assert.ok(document.activeElement === closeButton(), 'the replacing preview takes focus')
  click(closeButton())
  assert.ok(document.activeElement === draft, 'focus returns to Draft, not to the row of the preview it replaced')
})

// Each helper's read: its own transcript, or a failure for the one named.
function readsByHelper(failing) {
  globalThis.__dockingRead = async url => {
    const id = decodeURIComponent(url.split('/').at(-1))
    if (id === failing) return { ok: false, status: 500 }
    return { ok: true, status: 200, json: async () => ({ status: 'completed', provider: 'codex', blocks: [
      { type: 'tool', tool: 'Read', status: 'done', input: `notes-of-${id}.txt` },
    ] }) }
  }
}

for (const [caller, options, rowFor] of [
  ['the in-progress list', { foot: [helper('one', 'Survey', 'running'), helper('two', 'Draft', 'running')] },
    (pane, name) => rowNamed(pane, name)],
  ['a background-helper list', { subagent: {
    one: { description: 'Survey', status: 'done', summary: 'done' },
    two: { description: 'Draft', status: 'done', summary: 'done' },
  } }, (pane, name) => pane.querySelectorAll('button').find(node => node.getAttribute('aria-label') === `Open ${name} conversation`)],
]) {
  test(`switching helpers within ${caller} starts a fresh preview with the right transcript and return target`, async () => {
    readsByHelper('two')
    const { pane } = mountPane(1200, options)
    const survey = rowFor(pane, 'Survey')
    const draft = rowFor(pane, 'Draft')
    survey.focus()
    click(survey)
    await flush()
    assert.match(dialogs()[0].textContent, /Survey/)
    assert.match(dialogs()[0].textContent, /notes-of-one/, 'the first helper\'s transcript loads')
    draft.focus()
    click(draft)
    await flush()
    assert.equal(dialogs().length, 1)
    assert.match(dialogs()[0].textContent, /Draft/)
    assert.doesNotMatch(dialogs()[0].textContent, /notes-of-one/, 'a failed read never leaves the previous helper\'s transcript under the new title')
    assert.ok(dialogs()[0].querySelector('.helper-convo__state--error'), 'the failed read shows as an error')
    click(closeButton())
    assert.ok(document.activeElement === draft, 'closing returns focus to the row that opened this helper')
  })
}

// A chat-level panel such as the chat summary: modal, opened on demand.
let openPanel = () => {}
function ChatPanel() {
  const [shown, setShown] = useState(false)
  openPanel = () => setShown(true)
  return shown ? h(ChatPanelDialog) : null
}
function ChatPanelDialog() {
  const containerRef = useRef(null)
  const closeRef = useRef(null)
  useDialogFocus({ containerRef, initialFocusRef: closeRef, onClose: () => {} })
  return h('div', { className: 'chat-summary__overlay' },
    h('div', { ref: containerRef, role: 'dialog', 'aria-label': 'Chat summary' },
      h('button', { ref: closeRef, type: 'button' }, 'Close summary')))
}

test('narrowing the pane never blocks a chat panel opened over the docked preview', async () => {
  const { pane, resize } = mountPane(1200, { transcript: [helper('one', 'Survey')], panel: h(ChatPanel) })
  click(rowNamed(pane, 'Survey'))
  await flush()
  act(() => openPanel())
  await flush()
  const summary = pane.querySelector('.chat-summary__overlay')
  const summaryClose = summary.querySelector('button')
  assert.ok(document.activeElement === summaryClose, 'the summary takes focus over the docked preview')
  assert.equal(pane.querySelector('.helper-convo__overlay').inert, true, 'the summary blocks the preview')
  await resize(900)
  assert.notEqual(summary.inert, true, 'the narrowed preview leaves the newer summary usable')
  assert.equal(pane.querySelector('.chat__scroll').inert, true, 'while still covering the rest of its chat')
  assert.ok(document.activeElement === summaryClose, 'focus stays in the summary')
})

for (const width of [1200, 900]) {
  test(`Escape closes a ${width >= 1000 ? 'docked' : 'narrow'} preview after a click inside its transcript`, async t => {
    const listeners = new Set()
    t.mock.method(document, 'addEventListener', (type, listener) => { if (type === 'keydown') listeners.add(listener) })
    t.mock.method(document, 'removeEventListener', (type, listener) => { listeners.delete(listener) })
    const { pane } = mountPane(width, { transcript: [helper('one', 'Survey')] })
    click(rowNamed(pane, 'Survey'))
    await flush()
    const dialog = dialogs()[0]
    // A click on non-interactive text focuses the nearest focusable ancestor.
    assert.equal(dialog.getAttribute('tabindex'), '-1', 'the dialog itself can hold focus without being a tab stop')
    dialog.focus()
    const event = { key: 'Escape', target: document.activeElement, shiftKey: false, preventDefault() {} }
    act(() => { for (const listener of [...listeners]) listener(event) })
    assert.equal(dialogs().length, 0)
  })

  test(`a click in a ${width >= 1000 ? 'docked' : 'narrow'} transcript keeps keyboard scrolling and Escape`, async t => {
    const listeners = new Set()
    t.mock.method(document, 'addEventListener', (type, listener) => { if (type === 'keydown') listeners.add(listener) })
    t.mock.method(document, 'removeEventListener', (type, listener) => { listeners.delete(listener) })
    const { pane } = mountPane(width, { transcript: [helper('one', 'Survey')] })
    click(rowNamed(pane, 'Survey'))
    await flush()
    const body = dialogs()[0].querySelector('.helper-convo__body')
    // Text inside the scroller focuses the scroller, so the browser scrolls it.
    assert.equal(body.getAttribute('tabindex'), '0', 'the scrolling transcript takes focus from a click')
    body.focus()
    const event = { key: 'Escape', target: document.activeElement, shiftKey: false, preventDefault() {} }
    act(() => { for (const listener of [...listeners]) listener(event) })
    assert.equal(dialogs().length, 0)
  })
}

// A local dialog in another workspace pane, standing in for any pane-scoped
// dialog (an update review, an image lightbox).
function PaneDialog({ onClose }) {
  const containerRef = useRef(null)
  const boundaryRef = useRef(null)
  useDialogFocus({ containerRef, onClose, modal: false, inertBoundaryRef: boundaryRef })
  return h('div', { ref: boundaryRef, className: 'pane-b' },
    h('div', { ref: containerRef, role: 'dialog', 'aria-label': 'Other pane' }, h('button', { type: 'button' }, 'Inside')))
}

test('an open docked preview leaves a dialog in another pane its own Escape', async t => {
  const listeners = new Set()
  t.mock.method(document, 'addEventListener', (type, listener) => { if (type === 'keydown') listeners.add(listener) })
  t.mock.method(document, 'removeEventListener', (type, listener) => { listeners.delete(listener) })
  const press = target => {
    const event = { key: 'Escape', target, shiftKey: false, preventDefault() {} }
    act(() => { for (const listener of [...listeners]) listener(event) })
  }
  let otherClosed = 0
  const other = mount(h(PaneDialog, { onClose: () => { otherClosed += 1 } }))
  const { pane } = mountPane(1200, { transcript: [helper('one', 'Survey')] })
  click(rowNamed(pane, 'Survey'))
  await flush()
  assert.equal(dialogs().length, 2, 'the docked preview opened after the other pane\'s dialog')
  const inside = other.container.querySelector('button')
  inside.focus()
  press(inside)
  assert.equal(otherClosed, 1, 'Escape inside the other pane\'s dialog closes it although the preview is newer')
  assert.equal(pane.querySelectorAll('[role="dialog"]').length, 1, 'and leaves the preview open')
  press(pane.querySelector('.helper-convo__close'))
  assert.equal(pane.querySelectorAll('[role="dialog"]').length, 0, 'Escape inside the preview still closes the preview')
  assert.equal(otherClosed, 1)
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
