/* A truncated peer note reads whole on request, through either end's chat. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { renderHook } from './react-hook-shim.mjs'
import useFullPeerNote from '../useFullPeerNote.js'

const notFound = () => Object.assign(new Error('gone'), { status: 404 })

test('a read moves through loading to the whole note', async () => {
  let release
  const read = (chatId, messageId) => new Promise(resolve => { release = () => resolve({ body: `${chatId}:${messageId}` }) })
  const hook = renderHook(() => useFullPeerNote('chat-1', 'note-1', read))
  assert.equal(hook.result.current.readable, true)
  const pending = hook.result.current.load()
  assert.equal(hook.result.current.state, 'loading')
  release()
  await pending
  assert.equal(hook.result.current.state, 'idle')
  assert.equal(hook.result.current.full, 'chat-1:note-1')
})

test('a failed read shows an error and can be retried', async () => {
  let fail = true
  const read = async () => {
    if (fail) throw Object.assign(new Error('down'), { status: 500 })
    return { body: 'whole' }
  }
  const hook = renderHook(() => useFullPeerNote('chat-1', 'note-1', read))
  await hook.result.current.load()
  assert.equal(hook.result.current.state, 'error')
  assert.equal(hook.result.current.full, null)
  fail = false
  await hook.result.current.load()
  assert.equal(hook.result.current.full, 'whole')
})

test('a deleted sender chat falls back to the recipient chat', async () => {
  const asked = []
  const read = async chatId => {
    asked.push(chatId)
    if (chatId === 'deleted-sender') throw notFound()
    return { body: 'via recipient' }
  }
  const hook = renderHook(() => useFullPeerNote(['deleted-sender', 'recipient', null], 'note-1', read))
  await hook.result.current.load()
  assert.deepEqual(asked, ['deleted-sender', 'recipient'])
  assert.equal(hook.result.current.full, 'via recipient')
})

test('when no chat can read the note any more there is no endless retry', async () => {
  const asked = []
  const hook = renderHook(() => useFullPeerNote(['gone-sender', null], 'note-1', async chatId => {
    asked.push(chatId)
    throw notFound()
  }))
  await hook.result.current.load()
  assert.deepEqual(asked, ['gone-sender'])
  assert.equal(hook.result.current.state, 'unavailable')
  assert.equal(hook.result.current.readable, false)
})

test('without a message id or chat there is nothing to read', () => {
  assert.equal(renderHook(() => useFullPeerNote('chat-1', undefined, async () => ({}))).result.current.readable, false)
  assert.equal(renderHook(() => useFullPeerNote([null], 'note-1', async () => ({}))).result.current.readable, false)
})
