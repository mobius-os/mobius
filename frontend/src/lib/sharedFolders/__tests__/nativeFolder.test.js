import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'
import {
  addRememberedFolder, reconnectFolder, removeFolder, shareUploadFolder,
} from '../folderActions.js'
import { handleSharedFolderCommand, refreshPermissions } from '../folderHost.js'
import { loadFolderRecords } from '../folderStore.js'
import { folderState, hostStore, publish } from '../hostState.js'
import { NATIVE_CHUNK_BYTES, createNativeFolder, nativeFolderBridge } from '../nativeFolder.js'
import { BIN_FOLDER_NAME } from '../folderPaths.js'
import { shouldSuggestSharedFolder } from '../uploadSuggestion.js'
import { FakeNativeBridge, patternFile, textFile } from './fakeNativeBridge.js'

let bridge
let answers

beforeEach(() => {
  bridge = new FakeNativeBridge()
  globalThis.MobiusNative = bridge
  answers = []
  globalThis.indexedDB = new IDBFactory()
  globalThis.fetch = async (url, options) => {
    if (String(url).endsWith('/api/shared-folders/responses')) answers.push(JSON.parse(options.body))
    return new Response('{}', { status: 200 })
  }
  const store = hostStore()
  store.records = new Map()
  store.access = new Map()
  store.permissions = new Map()
  store.subscriptionId = 'stream-1'
  publish({ canPickNative: true, canRemember: true, status: 'ready' })
})

async function failsWith(promise, code) {
  await assert.rejects(promise, error => error.code === code)
}

function folderOver(tree, name = 'Docs') {
  return createNativeFolder(bridge, bridge.addFolder(name, tree))
}

test('only a phone app with the folder picker counts as the native bridge', () => {
  assert.equal(nativeFolderBridge({ setBadge() {} }), null, 'older app builds have MobiusNative without folders')
  assert.equal(nativeFolderBridge(null), null)
  assert.equal(nativeFolderBridge(bridge), bridge)
})

test('a native folder lists folders first, pages, and reads nothing up front', async () => {
  const folder = folderOver({
    'b.txt': textFile('bee', { modified: 7, type: 'text/plain' }),
    'a.txt': textFile('a'),
    Photos: { 'x.jpg': textFile('x') },
  })
  const first = await folder.run({ op: 'list', path: '', limit: 2 })
  assert.deepEqual(first.entries, [
    { name: 'Photos', kind: 'folder' },
    { name: 'a.txt', kind: 'file', size: 1, modified: 1 },
  ])
  assert.equal(first.total, 3)
  assert.equal(first.nextCursor, 2)
  const inner = await folder.run({ op: 'list', path: '/Photos/' })
  assert.deepEqual(inner.entries.map(entry => entry.name), ['x.jpg'])
  assert.equal(inner.path, 'Photos', 'the bridge gets paths without leading or trailing slashes')
  assert.deepEqual(bridge.reads, [], 'listing never reads file contents')
})

test('info describes files and counts a folder', async () => {
  const folder = folderOver({ 'b.txt': textFile('bee', { modified: 7, type: 'text/plain' }), Sub: { a: textFile('1'), b: textFile('2') } })
  assert.deepEqual(await folder.run({ op: 'info', path: 'b.txt' }), {
    path: 'b.txt', name: 'b.txt', kind: 'file', size: 3, modified: 7, type: 'text/plain',
  })
  assert.deepEqual(await folder.run({ op: 'info', path: 'Sub' }), { path: 'Sub', name: 'Sub', kind: 'folder', entries: 2 })
  assert.equal((await folder.run({ op: 'info', path: '' })).name, 'Docs')
})

test('a read takes only the requested slice of a large file', async () => {
  const size = 5 * 1024 * 1024
  const { bytes } = patternFile(size)
  const folder = folderOver({ 'big.bin': { bytes } })
  const offset = NATIVE_CHUNK_BYTES - 5
  const slice = await folder.run({ op: 'read', path: 'big.bin', offset, length: 10, encoding: 'base64' })
  assert.equal(slice.offset, offset)
  assert.equal(slice.length, 10)
  assert.equal(slice.size, size)
  assert.deepEqual(Uint8Array.from(atob(slice.base64), c => c.charCodeAt(0)), bytes.subarray(offset, offset + 10))
  assert.deepEqual(bridge.reads, [{ path: 'big.bin', offset, length: 10 }])
  const tail = await folder.run({ op: 'read', path: 'big.bin', fromEnd: true, length: 4, encoding: 'base64' })
  assert.equal(tail.offset, size - 4)
  assert.equal(tail.eof, true)
})

test('a whole file larger than one bridge call is read in 4 MB chunks and reassembled in order', async () => {
  const size = 2 * NATIVE_CHUNK_BYTES + 123
  const { bytes } = patternFile(size)
  const folder = folderOver({ 'photo.png': { bytes, type: 'image/png' } })
  let decoded = null
  globalThis.createImageBitmap = async (file) => {
    decoded = new Uint8Array(await file.arrayBuffer())
    throw new Error('no image decoder in tests')
  }
  try {
    await failsWith(folder.run({ op: 'image', path: 'photo.png' }), 'unsupported_image')
  } finally {
    delete globalThis.createImageBitmap
  }
  assert.deepEqual(bridge.reads.map(read => [read.offset, read.length]), [
    [0, NATIVE_CHUNK_BYTES], [NATIVE_CHUNK_BYTES, NATIVE_CHUNK_BYTES], [2 * NATIVE_CHUNK_BYTES, 123],
  ])
  assert.equal(decoded.length, size)
  assert.deepEqual(decoded, bytes)
})

test('a short bridge read is the end of the file, and an unknown size is asked for', async () => {
  // The listing claims no size, and the file is shorter than requested.
  const folder = folderOver({ 'log.txt': textFile('hello world', { size: null }) })
  const slice = await folder.run({ op: 'read', path: 'log.txt', length: 100 })
  assert.equal(slice.text, 'hello world')
  assert.equal(slice.size, 11)
  assert.equal(slice.eof, true)
})

test('a short chunk in the middle of a file does not cut the read short', async () => {
  const { bytes } = patternFile(10_000)
  const folder = folderOver({ 'log.bin': { bytes } })
  bridge.chunkLimit = 3000
  const slice = await folder.run({ op: 'read', path: 'log.bin', offset: 100, length: 9000, encoding: 'base64' })
  assert.equal(slice.length, 9000)
  assert.deepEqual(Uint8Array.from(atob(slice.base64), c => c.charCodeAt(0)), bytes.subarray(100, 9100))
  assert.deepEqual(bridge.reads.map(read => read.offset), [100, 3100, 6100])
})

test('a file shorter than its listing ends the read instead of asking forever', async () => {
  // The listing says 50 bytes, but only 11 are left when it is read.
  const folder = folderOver({ 'log.txt': textFile('hello world', { size: 50 }) })
  const slice = await folder.run({ op: 'read', path: 'log.txt', length: 50 })
  assert.equal(slice.text, 'hello world')
  assert.ok(bridge.reads.length <= 2)
})

test('base64 decodes the same without Uint8Array.fromBase64', async () => {
  const folder = folderOver({ 'č.txt': textFile('čćž') })
  const native = Uint8Array.fromBase64
  Uint8Array.fromBase64 = undefined
  try {
    assert.equal((await folder.run({ op: 'read', path: 'č.txt' })).text, 'čćž')
  } finally {
    Uint8Array.fromBase64 = native
  }
})

test('bridge failures become the folder errors agents already know', async () => {
  const folder = folderOver({ 'a.txt': textFile('a'), Sub: {} })
  await assert.rejects(folder.run({ op: 'read', path: 'missing.txt' }), error => (
    error.code === 'not_found' && error.message === 'Nothing is at "missing.txt" in this folder.'
  ))
  await failsWith(folder.run({ op: 'read', path: 'Sub' }), 'wrong_kind')
  await failsWith(folder.run({ op: 'list', path: 'a.txt' }), 'wrong_kind')
  await failsWith(folder.run({ op: 'read', path: '../escape' }), 'invalid_path')

  const broken = createNativeFolder({
    listFolder: async () => ({ error: { code: 'device_error', message: 'The SD card was removed.' } }),
    folderEntry: async () => ({ error: { code: 'something_new', message: 'Unexpected.' } }),
  }, 'x')
  await assert.rejects(broken.run({ op: 'list', path: '' }), error => (
    error.code === 'device_error' && error.message === 'The SD card was removed.'
  ))
  await failsWith(broken.run({ op: 'info', path: 'a' }), 'device_error')
})

test('an older phone app without writes keeps its folders read only, before reaching the app', async () => {
  const id = bridge.addFolder('Docs', { 'a.txt': textFile('a') })
  const olderApp = { pickFolder() {}, folderEntry: (...args) => bridge.folderEntry(...args) }
  const folder = createNativeFolder(olderApp, id)
  for (const op of ['write', 'move', 'mkdir', 'bin']) {
    await failsWith(folder.run({ op, path: 'a.txt', content: 'x', to: 'b.txt' }), 'read_only')
  }
})

test('a native replace leaves alone a file that changed while it was backed up', async () => {
  const tree = { 'todo.txt': textFile('milk') }
  const folder = folderOver(tree)
  const copy = bridge.copyFolderFile.bind(bridge)
  // A synced provider saves a newer version just after the backup copy.
  bridge.copyFolderFile = async (...args) => {
    const result = await copy(...args)
    tree['todo.txt'] = textFile('milk and honey', { modified: 2 })
    return result
  }
  await failsWith(
    folder.run({ op: 'write', path: 'todo.txt', content: 'eggs', overwrite: true }),
    'changed_during_write',
  )
  assert.equal(new TextDecoder().decode(tree['todo.txt'].bytes), 'milk and honey')
  assert.equal(new TextDecoder().decode(tree[BIN_FOLDER_NAME]['todo.txt'].bytes), 'milk')
})

test('the app refuses a native replace if the file changes after the page checked it', async () => {
  const tree = { 'todo.txt': textFile('milk') }
  const folder = folderOver(tree)
  const write = bridge.writeFolderFile.bind(bridge)
  const sent = []
  // Saved again on the phone between the page's check and the app's write.
  bridge.writeFolderFile = async (id, path, data, options) => {
    sent.push(options)
    tree['todo.txt'] = textFile('milk and honey', { modified: 2 })
    return write(id, path, data, options)
  }
  await failsWith(
    folder.run({ op: 'write', path: 'todo.txt', content: 'eggs', overwrite: true }),
    'changed_during_write',
  )
  assert.deepEqual(sent, [{ overwrite: true, expected: { size: 4, modified: 1 } }])
  assert.equal(new TextDecoder().decode(tree['todo.txt'].bytes), 'milk and honey')
})

test('a native folder shared to edit saves, moves and bins files, keeping replaced ones', async () => {
  const tree = { 'todo.txt': textFile('milk'), Notes: {} }
  const folder = folderOver(tree)
  assert.equal((await folder.run({ op: 'write', path: 'new.txt', content: 'hi' })).created, true)
  assert.equal(new TextDecoder().decode(tree['new.txt'].bytes), 'hi')
  await failsWith(folder.run({ op: 'write', path: 'todo.txt', content: 'eggs' }), 'exists')
  await failsWith(folder.run({ op: 'write', path: 'missing/a.txt', content: 'x' }), 'not_found')

  const replaced = await folder.run({ op: 'write', path: 'todo.txt', content: 'eggs', overwrite: true })
  assert.deepEqual([replaced.created, replaced.binned], [false, `${BIN_FOLDER_NAME}/todo.txt`])
  assert.equal(new TextDecoder().decode(tree[BIN_FOLDER_NAME]['todo.txt'].bytes), 'milk')
  const again = await folder.run({ op: 'write', path: 'todo.txt', content: 'bread', overwrite: true })
  assert.equal(again.binned, `${BIN_FOLDER_NAME}/todo (2).txt`)

  assert.deepEqual(await folder.run({ op: 'mkdir', path: 'Notes/2026' }), { path: 'Notes/2026', created: true })
  assert.equal((await folder.run({ op: 'mkdir', path: 'Notes' })).created, false)
  assert.deepEqual(
    await folder.run({ op: 'move', path: 'new.txt', to: 'Notes/hello.txt' }),
    { path: 'new.txt', to: 'Notes/hello.txt' },
  )
  assert.equal(tree['new.txt'], undefined)
  await failsWith(folder.run({ op: 'move', path: 'todo.txt', to: 'Notes/hello.txt' }), 'exists')
  await failsWith(folder.run({ op: 'move', path: 'Notes', to: 'Other' }), 'folder_move_unsupported')

  assert.deepEqual(await folder.run({ op: 'bin', path: 'Notes/hello.txt' }), {
    path: 'Notes/hello.txt', to: `${BIN_FOLDER_NAME}/hello.txt`,
  })
  assert.equal(tree.Notes['hello.txt'], undefined)
})

test('agents cannot change the bin or hidden names in a native folder', async () => {
  const folder = folderOver({ '.env': textFile('KEY=1'), [BIN_FOLDER_NAME]: { 'old.txt': textFile('old') } })
  await failsWith(folder.run({ op: 'write', path: '.env', content: 'x', overwrite: true }), 'hidden_name')
  await failsWith(folder.run({ op: 'write', path: `${BIN_FOLDER_NAME}/old.txt`, content: 'x', overwrite: true }), 'in_bin')
  await failsWith(folder.run({ op: 'bin', path: `${BIN_FOLDER_NAME}/old.txt` }), 'already_in_bin')
  await failsWith(folder.run({ op: 'move', path: '.env', to: 'env' }), 'hidden_name')
})

test('the app refusing a change reaches the agent as its code', async () => {
  const id = bridge.addFolder('Docs', { 'a.txt': textFile('a') }, 'read')
  await failsWith(createNativeFolder(bridge, id).run({ op: 'mkdir', path: 'new' }), 'read_only')
})

test('Add folder in the phone app picks natively with the chosen rights, and remembers it across reloads', async () => {
  bridge.willPick('Camera', { 'a.jpg': textFile('jpeg') })
  const outcome = await addRememberedFolder('edit')
  assert.equal(outcome.ok, true)
  assert.equal(outcome.name, 'Camera')
  assert.equal(outcome.notice, undefined)
  const [saved] = await loadFolderRecords()
  assert.equal(saved.source, 'native')
  assert.equal(saved.mode, 'edit')
  assert.equal(saved.access, 'remembered')
  assert.equal(saved.nativeId, 'native-1')
  assert.notEqual(saved.id, saved.nativeId, 'the page keeps its own id scheme')
  assert.equal(saved.handle, undefined)

  // A reload: only the saved record is left, and the bridge gives access back.
  const store = hostStore()
  store.access = new Map()
  store.permissions = new Map()
  store.records = new Map([[saved.id, saved]])
  await refreshPermissions({ report: false })
  assert.equal(folderState(saved), 'ready')
  await handleSharedFolderCommand({ commandId: 'c1', folderId: saved.id, op: 'read', path: 'a.jpg' })
  assert.equal(answers.at(-1).ok, true)
  assert.equal(answers.at(-1).result.text, 'jpeg')
})

test('a phone folder asked for with Read & edit is shared Read only when the app cannot change it', async () => {
  bridge.writable = false
  bridge.willPick('Drive', { 'a.txt': textFile('a') })
  const outcome = await addRememberedFolder('edit')
  assert.equal(outcome.ok, true)
  assert.match(outcome.notice, /Read only/)
  const [record] = hostStore().records.values()
  assert.equal(record.mode, 'read')
  await handleSharedFolderCommand({ commandId: 'c1', folderId: record.id, op: 'mkdir', path: 'new' })
  assert.equal(answers.at(-1).code, 'read_only')
})

test('Read only from the phone app stays read only, and the upload prompt never asks to edit', async () => {
  bridge.willPick('Docs', {})
  await addRememberedFolder('read')
  bridge.willPick('Docs', {})
  await shareUploadFolder()
  assert.deepEqual([...hostStore().records.values()].map(record => record.mode), ['read', 'read'])
})

test('cancelling the phone picker adds nothing', async () => {
  bridge.willPick('x', null)
  assert.deepEqual(await addRememberedFolder('read'), { ok: false, cancelled: true })
  assert.equal(hostStore().records.size, 0)
})

test('picking the same name twice keeps two folders rather than merging them', async () => {
  bridge.willPick('Docs', {})
  await shareUploadFolder()
  bridge.willPick('Docs', {})
  await shareUploadFolder()
  assert.equal(hostStore().records.size, 2)
})

test('when the app loses a folder, Pick again replaces its native id and forgets the old one', async () => {
  bridge.willPick('Docs', { 'a.txt': textFile('old') })
  await addRememberedFolder('read')
  const [record] = hostStore().records.values()
  bridge.revoke(record.nativeId)

  await handleSharedFolderCommand({ commandId: 'c1', folderId: record.id, op: 'read', path: 'a.txt' })
  assert.equal(answers.at(-1).ok, false)
  assert.equal(answers.at(-1).code, 'needs_permission')
  assert.match(answers.at(-1).error, /Pick again/)
  await refreshPermissions({ report: false })
  assert.equal(folderState(record), 'needs_permission')
  assert.equal(hostStore().state.folders[0].native, true)

  bridge.willPick('Docs', { 'a.txt': textFile('new') })
  assert.equal((await reconnectFolder(record.id)).ok, true)
  const updated = hostStore().records.get(record.id)
  assert.equal(updated.nativeId, 'native-2')
  assert.deepEqual(bridge.forgotten, [record.nativeId])
  assert.equal(folderState(updated), 'ready')
  await handleSharedFolderCommand({ commandId: 'c2', folderId: record.id, op: 'read', path: 'a.txt' })
  assert.equal(answers.at(-1).result.text, 'new')
  assert.equal((await loadFolderRecords())[0].nativeId, 'native-2')
})

test('a folder deleted on the phone asks the owner to Pick again instead of staying ready', async () => {
  bridge.willPick('Docs', { 'a.txt': textFile('a') })
  await addRememberedFolder('read')
  const [record] = hostStore().records.values()
  bridge.deleteFolder(record.nativeId)

  await refreshPermissions({ report: false })
  assert.equal(folderState(record), 'needs_permission')
  // Settings shows a native folder's needs_permission as Pick again.
  assert.equal(hostStore().state.folders[0].native, true)
  await handleSharedFolderCommand({ commandId: 'c1', folderId: record.id, op: 'read', path: 'a.txt' })
  assert.equal(answers.at(-1).code, 'needs_permission')
  assert.match(answers.at(-1).error, /Pick again/)

  bridge.willPick('Docs', { 'a.txt': textFile('restored') })
  assert.equal((await reconnectFolder(record.id)).ok, true)
  assert.equal(folderState(hostStore().records.get(record.id)), 'ready')
})

test('a phone folder the app answers for with an unknown failure is not taken as ready', async () => {
  bridge.willPick('Docs', {})
  await addRememberedFolder('read')
  const [record] = hostStore().records.values()
  bridge.folderEntry = async () => ({ error: { code: 'something_new', message: 'Unexpected.' } })

  await refreshPermissions({ report: false })
  assert.equal(folderState(record), 'needs_permission')
})

test('removing a native folder tells the app to forget it', async () => {
  bridge.willPick('Docs', {})
  await addRememberedFolder('read')
  const [record] = hostStore().records.values()
  await removeFolder(record.id)
  assert.deepEqual(bridge.forgotten, [record.nativeId])
  assert.deepEqual(await loadFolderRecords(), [])
})

test('the big-upload prompt offers sharing in the phone app only once it can pick folders', () => {
  const files = Array.from({ length: 6 }, () => ({ size: 1 }))
  assert.equal(shouldSuggestSharedFolder(files, { status: 'ready', canRemember: true, canPickNative: true }), true)
  globalThis.MobiusNative = { setBadge() {} }
  assert.equal(shouldSuggestSharedFolder(files, { status: 'ready', canRemember: false, canPickForVisit: true, canPickNative: false }), false)
})
