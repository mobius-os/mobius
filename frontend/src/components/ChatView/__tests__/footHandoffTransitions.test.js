/* What the reader opened in the foot's handoff stack stays open while rows
   arrive, leave, fold into the summary, or move between groups. */
import { after, afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { click, h, mount, unmountAll } from './mountedRender.js'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { TEST_MODEL_REGISTRY } from './modelRegistryRender.js'

// The dialog's contents fetch the helper's conversation; only its lifetime and
// the status it is given matter here.
const DIALOG_STUB = '\0helper-conversation-stub'
const dialogEvents = []
globalThis.__helperDialogEvents = dialogEvents
const vite = await createSsrRenderServer({
  plugins: [{
    name: 'stub-helper-conversation',
    enforce: 'pre',
    resolveId: source => source.endsWith('/HelperConversation.jsx') ? DIALOG_STUB : null,
    load: id => id === DIALOG_STUB ? `
      import { createElement, useEffect } from 'react'
      export default function HelperConversation({ taskId, status, onClose }) {
        useEffect(() => {
          globalThis.__helperDialogEvents.push('open ' + taskId)
          return () => globalThis.__helperDialogEvents.push('close ' + taskId)
        }, [taskId])
        return createElement('div', { role: 'dialog', 'data-task': taskId, 'data-status': status, onClick: onClose })
      }` : null,
  }],
})
after(() => vite.close())
const { default: WaitingChip } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')

const client = new QueryClient()
client.setQueryData(['models', 'registry'], TEST_MODEL_REGISTRY)
after(() => client.clear())
const withModels = node => h(QueryClientProvider, { client }, node)

const helper = (index, status = 'running') => ({
  delegation_id: `del-${index}`, task_key: `helper-${index}`, provider: 'claude', status,
})
const helperButton = (container, index) => container.querySelectorAll('button')
  .find(button => button.getAttribute('aria-label')?.startsWith(`helper-${index}:`))
const dialogs = container => container.querySelectorAll('[role="dialog"]')
const summaryButton = container => container.querySelector('.chat__handoff-summary').querySelector('button')

let chat = 0
function mountTray(props) {
  // A fresh chat id each time: the summarized list's open state is per chat.
  const chatId = `transition-chat-${++chat}`
  const view = mount(h(WaitingChip, { chatId, onCancel: async () => ({}), ...props }), { wrap: withModels })
  return { ...view, rerender: next => view.rerender(h(WaitingChip, { chatId, onCancel: async () => ({}), ...props, ...next })) }
}

beforeEach(() => { dialogEvents.length = 0 })
afterEach(unmountAll)

test('an open helper conversation stays open when a fourth helper folds the list into its summary', () => {
  const view = mountTray({ helpers: [0, 1, 2].map(i => helper(i)) })
  click(helperButton(view.container, 0))
  assert.equal(dialogs(view.container).length, 1)

  view.rerender({ helpers: [0, 1, 2, 3].map(i => helper(i)) })
  assert.ok(view.container.querySelector('.chat__handoff-summary'), 'four rows fold into one summary line')
  assert.equal(dialogs(view.container)[0]?.getAttribute('data-task'), 'del-0')
  assert.deepEqual(dialogEvents, ['open del-0'], 'the same dialog stayed mounted')
  view.unmount()
})

test('an open helper conversation stays open while its helper moves from Working to Waiting', () => {
  const view = mountTray({ helpers: [0, 1, 2, 3].map(i => helper(i)) })
  click(summaryButton(view.container))
  click(helperButton(view.container, 0))
  assert.equal(dialogs(view.container)[0]?.getAttribute('data-status'), 'running')

  view.rerender({ helpers: [helper(0, 'needs_input'), ...[1, 2, 3].map(i => helper(i))] })
  const group = helperButton(view.container, 0).closest('[role="group"]')
  assert.match(group.getAttribute('aria-label'), /^Waiting/)
  // Still the same conversation, now saying its helper waits on its coordinator.
  assert.equal(dialogs(view.container)[0]?.getAttribute('data-status'), 'waiting')
  assert.deepEqual(dialogEvents, ['open del-0'])
  view.unmount()
})

test('an open helper conversation stays open when another helper leaves and the summary unfolds', () => {
  const view = mountTray({ helpers: [0, 1, 2, 3].map(i => helper(i)) })
  click(summaryButton(view.container))
  click(helperButton(view.container, 1))

  view.rerender({ helpers: [0, 1, 2].map(i => helper(i)) })
  assert.equal(view.container.querySelector('.chat__handoff-summary'), null)
  assert.equal(dialogs(view.container)[0]?.getAttribute('data-task'), 'del-1')
  assert.deepEqual(dialogEvents, ['open del-1'])
  view.unmount()
})

test('a helper that leaves the tray closes its conversation and does not reopen it on return', () => {
  const view = mountTray({ helpers: [0, 1].map(i => helper(i)) })
  click(helperButton(view.container, 1))

  view.rerender({ helpers: [helper(0)] })
  assert.equal(dialogs(view.container).length, 0)
  view.rerender({ helpers: [0, 1].map(i => helper(i)) })
  assert.equal(dialogs(view.container).length, 0, 'a returning row is closed until tapped again')
  assert.deepEqual(dialogEvents, ['open del-1', 'close del-1'])

  click(helperButton(view.container, 0))
  click(dialogs(view.container)[0])
  assert.equal(dialogs(view.container).length, 0, 'closing the dialog closes it')
  view.unmount()
})

const commandWait = { id: 'wait-c', kind: 'command', description: 'Preview responds', command: 'curl -fsS https://preview.test/healthz', interval_secs: 300 }
const waitToggle = container => container.querySelectorAll('button')
  .find(button => button.getAttribute('aria-label')?.includes('waiting details: Waiting · Preview responds'))
const button = (container, text) => container.querySelectorAll('button').find(node => node.textContent === text)

test('an expanded wait keeps its revealed command when helpers fold the list and it changes group', () => {
  const view = mountTray({ helpers: [helper(0)], waits: [commandWait] })
  click(waitToggle(view.container))
  click(button(view.container, 'Show'))
  assert.ok(view.container.querySelector('.chat__wait-command-text'))

  view.rerender({ helpers: [0, 1, 2].map(i => helper(i)) })
  click(summaryButton(view.container))
  assert.ok(view.container.querySelector('.chat__wait-command-text'), 'folding into the summary keeps the command shown')

  view.rerender({ helpers: [0, 1, 2].map(i => helper(i)), waits: [{ ...commandWait, delivery_pending: true, resume_blocker: 'owner_input' }] })
  assert.match(view.container.querySelector('.chat__wait-command-text').closest('[role="group"]').getAttribute('aria-label'), /^Needs you/)
  assert.ok(button(view.container, 'Hide'))
  view.unmount()
})

test('a wait being stopped stays stopping, and a refused stop keeps its reason, across regrouping', async () => {
  let settle
  const onCancel = () => new Promise(resolve => { settle = resolve })
  const view = mountTray({ helpers: [helper(0)], waits: [commandWait], onCancel })
  click(waitToggle(view.container))
  click(button(view.container, 'Stop waiting'))
  assert.equal(button(view.container, 'Stopping…')?.hasAttribute('disabled'), true)

  view.rerender({ helpers: [0, 1, 2].map(i => helper(i)), onCancel })
  click(summaryButton(view.container))
  assert.equal(button(view.container, 'Stopping…')?.hasAttribute('disabled'), true, 'a remount must not offer Stop again mid-request')

  const { act } = await import('react')
  await act(async () => settle({ disposition: 'not_withdrawable', detail: 'Its result is already on the way.' }))
  view.rerender({ helpers: [0, 1].map(i => helper(i)), onCancel })
  assert.equal(view.container.querySelector('.chat__handoff-summary'), null)
  assert.match(view.container.querySelector('.chat__wait-error').textContent, /already on the way/)
  view.unmount()
})
