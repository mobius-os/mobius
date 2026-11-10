/* A truncated text reads whole on request, through the first reader that can. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderHook } from './react-hook-shim.mjs'
import useFullPeerNote from '../useFullPeerNote.js'

const notFound = () => Object.assign(new Error('gone'), { status: 404 })

test('a read moves through loading to the whole text', async () => {
  let release
  const readers = [() => new Promise(resolve => { release = () => resolve('the whole note') })]
  const hook = renderHook(() => useFullPeerNote(readers))
  assert.equal(hook.result.current.readable, true)
  const pending = hook.result.current.load()
  assert.equal(hook.result.current.state, 'loading')
  release()
  await pending
  assert.equal(hook.result.current.state, 'idle')
  assert.equal(hook.result.current.full, 'the whole note')
})

test('a failed read shows an error and can be retried', async () => {
  let fail = true
  const readers = [async () => {
    if (fail) throw Object.assign(new Error('down'), { status: 500 })
    return 'whole'
  }]
  const hook = renderHook(() => useFullPeerNote(readers))
  await hook.result.current.load()
  assert.equal(hook.result.current.state, 'error')
  assert.equal(hook.result.current.full, null)
  fail = false
  await hook.result.current.load()
  assert.equal(hook.result.current.full, 'whole')
})

test('a reader that finds nothing (404) falls back to the next one', async () => {
  const asked = []
  const readers = [
    async () => { asked.push('deleted-sender'); throw notFound() },
    async () => { asked.push('recipient'); return 'via recipient' },
  ]
  const hook = renderHook(() => useFullPeerNote(readers))
  await hook.result.current.load()
  assert.deepEqual(asked, ['deleted-sender', 'recipient'])
  assert.equal(hook.result.current.full, 'via recipient')
})

test('when no reader can find the text any more there is no endless retry', async () => {
  const asked = []
  const hook = renderHook(() => useFullPeerNote([async () => { asked.push('gone-sender'); throw notFound() }]))
  await hook.result.current.load()
  assert.deepEqual(asked, ['gone-sender'])
  assert.equal(hook.result.current.state, 'unavailable')
  assert.equal(hook.result.current.readable, false)
})

test('without a reader there is nothing to read', () => {
  assert.equal(renderHook(() => useFullPeerNote([])).result.current.readable, false)
})
