/* Exercise the real preview's read/refresh lifetime after its tray row leaves.
   Only its HTTP boundary is stubbed; no live backend or browser is involved. */
import { after, afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { act } from 'react'
import { click, h, mount, unmountAll } from './mountedRender.js'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { TEST_MODEL_REGISTRY } from './modelRegistryRender.js'

const API_STUB = '\0preview-http'
const vite = await createSsrRenderServer({ plugins: [{
  name: 'preview-http', enforce: 'pre',
  resolveId: (source, importer) => importer?.endsWith('/HelperConversation.jsx') && source.endsWith('/api/client.js') ? API_STUB : null,
  load: id => id === API_STUB ? 'export const apiFetch = (...args) => globalThis.__previewRead(...args)' : null,
}] })
after(() => vite.close())
const { default: WaitingChip } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
const client = new QueryClient()
client.setQueryData(['models', 'registry'], TEST_MODEL_REGISTRY)
after(() => client.clear())
afterEach(() => { unmountAll(); delete globalThis.__previewRead })

const helper = { delegation_id: 'helper', task_key: 'Review', provider: 'codex', status: 'running' }
const dialog = () => document.body.querySelector('[role="dialog"]')
const row = container => container.querySelectorAll('button').find(node => node.getAttribute('aria-label')?.startsWith('Review:'))
const flush = () => act(async () => {})

for (const [initialStatus, finalStatus] of [['running', 'needs_review'], ['needs_input', 'completed'], ['paused', 'completed']]) {
  test(`a ${initialStatus} preview survives departure, learns resumption and settlement, and releases refresh on close`, async t => {
    let status = initialStatus
    const reads = []
    globalThis.__previewRead = async (url, { signal }) => {
      reads.push({ url, signal })
      return { ok: true, json: async () => ({ status, provider: 'codex', blocks: [], child_chat_id: 'child' }) }
    }
    const timers = new Map()
    const originalSet = globalThis.setTimeout
    const originalClear = globalThis.clearTimeout
    t.mock.method(globalThis, 'setTimeout', (callback, delay, ...args) => {
      if (delay !== 3000) return originalSet(callback, delay, ...args)
      const id = Symbol('preview timer')
      timers.set(id, callback)
      return id
    })
    t.mock.method(globalThis, 'clearTimeout', id => {
      if (timers.has(id)) timers.delete(id)
      else if (typeof id !== 'symbol') originalClear(id)
    })
    const view = mount(h(WaitingChip, { chatId: 'parent', helpers: [{ ...helper, status: initialStatus }] }), {
      wrap: node => h(QueryClientProvider, { client }, node),
    })
    click(row(view.container))
    await flush()
    const opened = dialog()
    assert.match(opened.textContent, initialStatus === 'running' ? /Working/ : initialStatus === 'paused' ? /On hold/ : /Waiting for coordinating agent/)
    assert.equal(timers.size, 1)
    view.rerender(h(WaitingChip, { chatId: 'parent', helpers: [], hidden: true }))
    assert.equal(dialog(), opened, 'neither departure nor owner-card hiding remounts the preview')
    status = 'running'
    await act(async () => {
      const [id, tick] = timers.entries().next().value
      timers.delete(id)
      await tick()
    })
    assert.equal(dialog(), opened)
    assert.match(dialog().textContent, /Working/)
    assert.equal(timers.size, 1, 'held helpers keep observing after resumption')
    status = finalStatus
    await act(async () => {
      const [id, tick] = timers.entries().next().value
      timers.delete(id)
      await tick()
    })
    assert.equal(dialog(), opened)
    assert.match(dialog().textContent, finalStatus === 'completed' ? /Finished/ : /Needs review/)
    assert.doesNotMatch(dialog().textContent, /Still working/)
    assert.equal(timers.size, 0, 'the endpoint settles polling without an intermediate completed tray row')
    assert.ok(reads.every(read => read.url === '/chats/parent/helpers/helper'))
    click(dialog().querySelectorAll('button').find(node => node.getAttribute('aria-label') === 'Close helper conversation'))
    assert.equal(dialog(), null)
    assert.ok(reads.at(-1).signal.aborted)
    view.rerender(h(WaitingChip, { chatId: 'parent', helpers: [helper] }))
    assert.equal(dialog(), null, 'returning does not reopen the preview')
    for (const action of ['close', 'switch']) {
      status = initialStatus
      click(row(view.container))
      await flush()
      assert.equal(timers.size, 1)
      if (action === 'close') click(dialog().querySelectorAll('button').find(node => node.getAttribute('aria-label') === 'Close helper conversation'))
      else view.rerender(h(WaitingChip, { chatId: 'another-parent', helpers: [] }))
      assert.equal(dialog(), null)
      assert.equal(timers.size, 0, `${action} releases unfinished helper refresh`)
      assert.ok(reads.at(-1).signal.aborted)
    }
  })

}
