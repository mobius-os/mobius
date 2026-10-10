import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../hooks/__tests__/react-hook-shim.mjs'
import useFileUpload from '../useFileUpload.js'
import { persistComposerDraft, readComposerDraft } from '../composerDraft.js'

function setup(t, initialFiles = []) {
  const calls = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (url.endsWith('/upload-limits')) {
      return Promise.resolve({ ok: true, json: async () => ({ max_bytes: 50 * 1024 * 1024 }) })
    }
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
const tick = () => new Promise(resolve => setImmediate(resolve))

for (const action of ['remove', 'unmount']) {
  test(`${action} during limit discovery never starts an upload`, async t => {
    let resolveLimit
    let posts = 0
    t.mock.method(globalThis, 'fetch', (url, options) => {
      if (url.endsWith('/upload-limits')) return new Promise(resolve => { resolveLimit = resolve })
      if (options.method === 'POST') posts++
      return Promise.resolve({ok: true, json: async () => [record('server.txt')]})
    })
    const hook = renderHook(() => useFileUpload({ chatId: 'chat' }))
    const pending = hook.result.current.addFiles([uploadedFile()])
    await tick()
    if (action === 'remove') hook.result.current.removeFile(hook.result.current.files[0].id)
    else hook.unmount()
    resolveLimit({ok: true, json: async () => ({max_bytes: 50 * 1024 * 1024})})
    await pending
    assert.equal(posts, 0)
    if (action === 'remove') hook.unmount()
  })
}

test('failed limit discovery stays visible and a new selection can retry it', async t => {
  let checks = 0
  let posts = 0
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.endsWith('/upload-limits')) return ++checks === 1
      ? {ok: false}
      : {ok: true, json: async () => ({max_bytes: 50 * 1024 * 1024})}
    if (options.method === 'POST') posts++
    return {ok: true, json: async () => [record('server.txt')]}
  })
  const hook = renderHook(() => useFileUpload({ chatId: 'chat' }))
  await hook.result.current.addFiles([uploadedFile()])
  assert.match(hook.result.current.files[0].error, /Could not check upload limit/)
  assert.equal(posts, 0)
  await hook.result.current.addFiles([uploadedFile()])
  assert.equal(checks, 2)
  assert.equal(posts, 1)
  hook.unmount()
})

test('picker and paste share preflight: oversized originals never POST', async t => {
  const { hook, calls } = setup(t)
  const tooLarge = uploadedFile()
  Object.defineProperty(tooLarge, 'size', { value: 50 * 1024 * 1024 + 1 })
  await hook.result.current.addFiles([tooLarge])
  assert.equal(calls.length, 0)
  assert.equal(hook.result.current.files[0].status, 'error')
  assert.match(hook.result.current.files[0].error, /50 MiB upload limit/)
  hook.unmount()
})

test('the exact server-advertised file limit remains eligible for upload', async t => {
  const { hook, calls } = setup(t)
  const atLimit = uploadedFile()
  Object.defineProperty(atLimit, 'size', { value: 50 * 1024 * 1024 })
  const pending = hook.result.current.addFiles([atLimit])
  await tick()
  assert.equal(calls.length, 1)
  calls[0].resolve({ ok: true, json: async () => [record('server.txt')] })
  await pending
  hook.unmount()
})

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
    await tick()
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
  await tick()
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
  await tick()
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
  await tick()
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
