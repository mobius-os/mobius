import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import { useChatSummaryContinuity } from '../useChatSummaryContinuity.js'

const firstPage = {
  title: 'Name', summary: 'Current handoff',
  entries: [{ revision: 1, digest: 'first' }],
  next_after_revision: 1, has_more: true,
}

const settle = () => new Promise(resolve => setImmediate(resolve))

test('loads name, current summary and first digest page, then appends one bounded page', async () => {
  const reads = []
  const readPage = async (_chatId, afterRevision, signal) => {
    reads.push({ afterRevision, signal })
    return afterRevision === 0
      ? firstPage
      : { entries: [{ revision: 2, digest: 'second' }], next_after_revision: 2, has_more: false }
  }
  const hook = renderHook(useChatSummaryContinuity, 'chat', readPage)
  await settle()
  assert.equal(hook.result.current.state.layers.description, 'Name')
  assert.equal(hook.result.current.state.layers.summary, 'Current handoff')
  assert.deepEqual(hook.result.current.state.layers.history.map(row => row.digest), ['first'])
  assert.equal(hook.result.current.state.hasMore, true)

  await hook.result.current.loadMore()
  assert.deepEqual(reads.map(read => read.afterRevision), [0, 1])
  assert.deepEqual(hook.result.current.state.layers.history.map(row => row.digest), ['first', 'second'])
  assert.equal(hook.result.current.state.hasMore, false)
  hook.unmount()
})

test('blocks duplicate pages and safely resets an A/B/A chat switch', async () => {
  let resolveOlder
  const reads = []
  const readPage = (chatId, afterRevision, signal) => {
    reads.push({ chatId, afterRevision, signal })
    if (chatId === 'old' && afterRevision === 1) {
      return new Promise(resolve => { resolveOlder = resolve })
    }
    return Promise.resolve(chatId === 'old' ? firstPage : {
      title: 'New name', summary: 'New summary',
      entries: [{ revision: 1, digest: 'new' }],
      next_after_revision: 1, has_more: false,
    })
  }
  const hook = renderHook(useChatSummaryContinuity, 'old', readPage)
  await settle()
  const firstLoad = hook.result.current.loadMore()
  const duplicateLoad = hook.result.current.loadMore()
  assert.equal(reads.filter(read => read.chatId === 'old' && read.afterRevision === 1).length, 1)
  assert.equal(hook.result.current.loadingOlder, true)

  hook.rerender('new', readPage)
  assert.equal(reads.at(-1).chatId, 'new')
  assert.equal(hook.result.current.loadingOlder, false)
  assert.equal(reads.find(read => read.chatId === 'old' && read.afterRevision === 1).signal.aborted, true)
  await settle()
  resolveOlder({ entries: [{ revision: 2, digest: 'stale old' }], has_more: false, next_after_revision: 2 })
  await Promise.all([firstLoad, duplicateLoad])
  assert.equal(hook.result.current.state.layers.description, 'New name')
  assert.deepEqual(hook.result.current.state.layers.history.map(row => row.digest), ['new'])

  hook.rerender('old', readPage)
  await settle()
  assert.equal(hook.result.current.state.layers.description, 'Name')
  assert.equal(hook.result.current.loadingOlder, false)
  assert.deepEqual(hook.result.current.state.layers.history.map(row => row.digest), ['first'])
  hook.unmount()
})

test('empty history is represented as a valid ready page', async () => {
  const hook = renderHook(useChatSummaryContinuity, 'empty', async () => ({
    title: 'Empty', summary: '', entries: [], next_after_revision: 0, has_more: false,
  }))
  await settle()
  assert.equal(hook.result.current.state.status, 'ready')
  assert.deepEqual(hook.result.current.state.layers.history, [])
  assert.equal(hook.result.current.state.hasMore, false)
  hook.unmount()
})

test('preserves legacy raw baseline alongside its journal digest', async () => {
  const legacy = {
    revision: 0,
    checkpoint_id: 'legacy-unmigrated',
    legacy_baseline: true,
    digest: 'Historical continuity baseline.',
    legacy_markdown: '## Summary\n\nComplete legacy handoff.',
  }
  const hook = renderHook(useChatSummaryContinuity, 'legacy', async () => ({
    title: 'Old chat', summary: 'Current legacy handoff',
    entries: [legacy], next_after_revision: 0, has_more: false,
  }))
  await settle()
  assert.equal(hook.result.current.state.layers.history[0].legacy_markdown, legacy.legacy_markdown)
  assert.equal(hook.result.current.state.layers.history[0].digest, legacy.digest)
  hook.unmount()
})

test('a later successful page clears the previous pagination error', async () => {
  let attempts = 0
  const hook = renderHook(useChatSummaryContinuity, 'retry', async (_id, revision) => {
    if (revision === 0) return firstPage
    attempts += 1
    if (attempts === 1) throw new Error('Temporary read failure')
    return { entries: [{ revision: 2, digest: 'recovered' }], next_after_revision: 2, has_more: false }
  })
  await settle()
  await hook.result.current.loadMore()
  assert.match(hook.result.current.state.error, /Temporary read failure/)
  await hook.result.current.loadMore()
  assert.equal(hook.result.current.state.error, '')
  assert.deepEqual(hook.result.current.state.layers.history.map(row => row.digest), ['first', 'recovered'])
  hook.unmount()
})
