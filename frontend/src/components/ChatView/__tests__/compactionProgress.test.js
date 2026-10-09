/* Manual compaction never consumes composer state or silently starts another batch. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { api } from '../../../api/client.js'
import CompactionProgress from '../CompactionProgress.jsx'

function render(progress, busy = false) {
  return renderToStaticMarkup(createElement(CompactionProgress, {
    progress, busy, onContinue() {}, onStartOver() {}, onStop() {},
  }))
}

test('compaction routes preserve bodyless legacy calls and bind batch actions to one chat', async t => {
  const original = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return Response.json({ ok: true, progress: null })
  }
  t.after(() => { globalThis.fetch = original })
  await api.chats.compact('one/two')
  await api.chats.compact('one/two', { instructions: 'Keep decisions', batch_id: 'uuid-a' })
  await api.chats.compact('one/two', { batch_id: 'uuid-b', recovery_id: 'draft-1' })
  await api.chats.compactProgress('one/two')
  await api.chats.compactStop('one/two')
  assert.equal(requests[0].options.body, undefined)
  assert.deepEqual(JSON.parse(requests[1].options.body), { instructions: 'Keep decisions', batch_id: 'uuid-a' })
  assert.deepEqual(JSON.parse(requests[2].options.body), { batch_id: 'uuid-b', recovery_id: 'draft-1' })
  assert.deepEqual(requests.map(({ url, options }) => [url, options.method]), [
    ['/api/chats/one%2Ftwo/compact', 'POST'],
    ['/api/chats/one%2Ftwo/compact', 'POST'],
    ['/api/chats/one%2Ftwo/compact', 'POST'],
    ['/api/chats/one%2Ftwo/compact-progress', undefined],
    ['/api/chats/one%2Ftwo/compact-stop', 'POST'],
  ])
})

test('paused and interrupted batches require an explicit Continue and explain allowance', () => {
  for (const state of ['paused', 'interrupted']) {
    const html = render({ state, next_chunk: 2, total_chunks: 9, recovery_id: 'draft-1' })
    assert.match(html, /Completed 2 of 9 sections/)
    assert.match(html, /Continue compaction/)
    assert.match(html, /up to eight summarizing requests/)
    assert.match(html, /previous session stays in place/)
    assert.doesNotMatch(html, /Start over/)
  }
})

test('source stale offers only Start over while a running batch offers Pause', () => {
  const stale = render({ state: 'stale', next_chunk: 3, total_chunks: 8 })
  assert.match(stale, /Compaction source changed/)
  assert.match(stale, /Start over/)
  assert.doesNotMatch(stale, /Continue compaction/)
  const running = render({ state: 'running', next_chunk: 1, total_chunks: 8 })
  assert.match(running, /Pause compaction/)
  assert.doesNotMatch(running, /Continue compaction/)
  assert.equal(render(null), '')
})

test('each recovery state invokes only the explicitly selected action', () => {
  for (const [state, expected] of [['paused', 'continue'], ['interrupted', 'continue'], ['stale', 'start'], ['running', 'stop']]) {
    const calls = []
    const element = CompactionProgress({
      progress: { state, next_chunk: 8, total_chunks: 11 }, busy: false,
      onContinue: () => calls.push('continue'),
      onStartOver: () => calls.push('start'), onStop: () => calls.push('stop'),
    })
    assert.deepEqual(calls, [], 'rendering progress must never start work')
    const actionArea = element.props.children[1]
    const button = actionArea.props.children
    assert.equal(button.type, 'button')
    button.props.onClick()
    assert.deepEqual(calls, [expected])
  }
})
