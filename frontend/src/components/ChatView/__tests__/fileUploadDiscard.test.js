import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import useFileUpload from '../useFileUpload.js'
import { persistComposerDraft, readComposerDraft } from '../composerDraft.js'

function setup(t, initialFiles = []) {
  const calls = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (options.method === 'DELETE') {
      calls.push({ url, options })
      return Promise.resolve({ ok: true })
    }
    return new Promise(resolve => calls.push({ url, options, resolve }))
  })
  const hook = renderHook(() => useFileUpload({ chatId: 'chat', initialFiles }))
  return { hook, calls }
}
const record = name => ({ name, size: 3, mime_type: 'text/plain', status: 'done' })
const uploadedFile = () => new File(['abc'], 'local.txt', { type: 'text/plain' })

test('discard asks the server to drop every held draft, once', t => {
  const { hook, calls } = setup(t, [record('unused.txt'), record('accepted.txt')])
  hook.result.current.discardFiles()
  hook.result.current.discardFiles()
  assert.deepEqual(calls.map(call => call.url.split('/').pop()), [
    'unused.txt', 'accepted.txt',
  ])
  assert.deepEqual(hook.result.current.files, [])
  hook.unmount()
})

for (const action of ['remove', 'discard']) {
  test(`${action} during upload discards late success using server metadata`, async t => {
    const { hook, calls } = setup(t)
    const pending = hook.result.current.addFiles([uploadedFile()])
    if (action === 'remove') hook.result.current.removeFile(hook.result.current.files[0].id)
    else hook.result.current.discardFiles()
    assert.equal(calls.length, 1)
    calls[0].resolve({ ok: true, json: async () => [record('server_1.txt')] })
    await pending
    assert.deepEqual(hook.result.current.files, [])
    assert.match(calls[1].url, /server_1.txt$/)
    hook.unmount()
  })
}

test('navigation/unmount preserves completed drafts but discards orphaned late success', async t => {
  const { hook, calls } = setup(t, [record('draft.txt')])
  const pending = hook.result.current.addFiles([uploadedFile()])
  hook.unmount()
  calls[0].resolve({ ok: true, json: async () => [record('server.txt')] })
  await pending
  assert.equal(calls.length, 2)
  assert.match(calls[1].url, /server.txt$/)
  assert.ok(!calls.some(call => call.url.includes('draft.txt')))
})

test('server metadata replaces browser guesses', async t => {
  const { hook, calls } = setup(t)
  const pending = hook.result.current.addFiles([uploadedFile()])
  calls[0].resolve({ ok: true, json: async () => [record('server.txt')] })
  await pending
  assert.equal(hook.result.current.files[0].name, 'server.txt')
  hook.unmount()
})

test('removing an attachment restored from a saved draft still discards it on the server', async t => {
  const values = new Map()
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const { hook, calls } = setup(t)
  const pending = hook.result.current.addFiles([uploadedFile()])
  calls[0].resolve({ ok: true, json: async () => [record('server.txt')] })
  await pending
  persistComposerDraft('chat', '', hook.result.current.files, storage)
  hook.unmount()

  // Restoration is persisted again on mount; the restored chip must stay removable.
  const restored = readComposerDraft('chat', storage)
  persistComposerDraft('chat', restored.input, restored.attachments, storage)
  const second = readComposerDraft('chat', storage)
  const remounted = renderHook(() => useFileUpload({ chatId: 'chat', initialFiles: second.attachments }))
  try {
    remounted.result.current.removeFile(remounted.result.current.files[0].id)
    assert.deepEqual(remounted.result.current.files, [])
    assert.equal(calls.length, 2)
    assert.match(calls[1].url, /server.txt$/)
  } finally {
    remounted.unmount()
  }
})


test('discard never deletes by the local name of a failed upload', t => {
  const { hook, calls } = setup(t, [
    record('done.txt'),
    { name: 'image.png', status: 'error', error: 'Upload failed' },
  ])
  hook.result.current.discardFiles()
  assert.deepEqual(calls.map(call => call.url.split('/').pop()), ['done.txt'])
  hook.unmount()
})
