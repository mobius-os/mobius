import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createFoldersProvider, pathSegments } from '../localFolders.js'

// A tiny in-memory stand-in for FileSystemDirectoryHandle / FileSystemFileHandle.
function fakeDir(name, tree, state = { permission: 'granted', asked: 0 }) {
  const handle = {
    kind: 'directory',
    name,
    state,
    async queryPermission() { return state.permission },
    async requestPermission() { state.asked += 1; state.permission = state.onRequest || 'granted'; return state.permission },
    async isSameEntry(other) { return other === handle || other?.tree === tree },
    tree,
    async getDirectoryHandle(child) {
      const value = tree[child]
      if (!value || value instanceof Uint8Array) throw new Error('NotFoundError')
      return fakeDir(child, value, state)
    },
    async getFileHandle(child) {
      const value = tree[child]
      if (!(value instanceof Uint8Array)) throw new Error('NotFoundError')
      return {
        kind: 'file',
        name: child,
        async getFile() {
          return {
            name: child, size: value.byteLength, type: child.endsWith('.jpg') ? 'image/jpeg' : '',
            lastModified: 1700000000000,
            async arrayBuffer() { return value.slice().buffer },
          }
        },
      }
    },
    async *entries() {
      for (const child of Object.keys(tree)) {
        yield [child, tree[child] instanceof Uint8Array
          ? await handle.getFileHandle(child)
          : await handle.getDirectoryHandle(child)]
      }
    },
  }
  return handle
}

function memoryStore() {
  const rows = new Map()
  return {
    rows,
    async all() { return [...rows.values()] },
    async get(id) { return rows.get(id) },
    async put(record) { rows.set(record.id, record) },
    async remove(id) { rows.delete(id) },
  }
}

function run(provider, input, limits = {}) {
  return new Promise((resolve, reject) => {
    const channel = {
      ready() {},
      event() {},
      result(value, transfer) { resolve({ value, transfer }) },
      error: reject,
    }
    Promise.resolve(provider.open({ input, declaration: { limits }, channel })).catch(reject)
  })
}

const scans = () => ({
  'letter-1.jpg': new Uint8Array([1, 2, 3]),
  box: { 'letter-2.jpg': new Uint8Array([4, 5]), '.DS_Store': new Uint8Array([0]) },
})

function setup({ dialogs = [], tree = scans(), appId = 10 } = {}) {
  const store = memoryStore()
  const root = fakeDir('Scans', tree)
  const asked = []
  const win = { showDirectoryPicker: async () => root }
  const make = (id) => createFoldersProvider({
    appId: id,
    getAppName: () => `App ${id}`,
    store,
    win,
    requestDialog: async (details) => { asked.push(details); return dialogs.shift() ?? null },
  })
  return { store, root, asked, provider: make(appId), make }
}

test('connecting a folder needs the owner to choose it in the shell dialog', async () => {
  const { provider, asked, store } = setup({ dialogs: [{ pick: true }] })
  const { value } = await run(provider, { operation: 'connect' })
  assert.equal(value.name, 'Scans')
  assert.equal(asked[0].kind, 'connect')
  assert.equal(asked[0].appName, 'App 10')
  assert.deepEqual([...store.rows.values()][0].apps, ['10'])
  const listed = await run(provider, { operation: 'list' })
  assert.deepEqual(listed.value, { supported: true, folders: [{ id: value.id, name: 'Scans', ready: true }] })
})

test('cancelling the dialog connects nothing', async () => {
  const { provider, store } = setup({ dialogs: [null] })
  await assert.rejects(run(provider, { operation: 'connect' }), { code: 'aborted' })
  assert.equal(store.rows.size, 0)
})

test('another app cannot use a folder until the owner connects it for that app too', async () => {
  const { provider, make } = setup({ dialogs: [{ pick: true }] })
  const { value } = await run(provider, { operation: 'connect' })
  const other = make(9)
  assert.deepEqual((await run(other, { operation: 'list' })).value.folders, [])
  await assert.rejects(
    run(other, { operation: 'read', folderId: value.id, path: 'letter-1.jpg' }),
    { code: 'denied' },
  )
})

test('reusing a connected folder for a second app keeps one record with both apps', async () => {
  const { provider, store } = setup({ dialogs: [{ pick: true }] })
  const { value } = await run(provider, { operation: 'connect' })
  const dialogs = [{ folderId: value.id }]
  const other = createFoldersProvider({
    appId: 9, store, win: { showDirectoryPicker: async () => { throw new Error('should not pick') } },
    requestDialog: async (details) => {
      assert.deepEqual(details.folders, [{ id: value.id, name: 'Scans', connected: false }])
      return dialogs.shift()
    },
  })
  const reused = await run(other, { operation: 'connect' })
  assert.equal(reused.value.id, value.id)
  assert.deepEqual(store.rows.get(value.id).apps, ['10', '9'])
})

test('listing walks subfolders and reads return the bytes as a transfer', async () => {
  const { provider } = setup({ dialogs: [{ pick: true }] })
  const { value: folder } = await run(provider, { operation: 'connect' })
  const flat = await run(provider, { operation: 'entries', folderId: folder.id })
  assert.deepEqual(flat.value.entries.map((e) => [e.path, e.kind]), [['letter-1.jpg', 'file'], ['box', 'directory']])
  const deep = await run(provider, { operation: 'entries', folderId: folder.id, recursive: true })
  assert.deepEqual(deep.value.entries.map((e) => e.path), ['letter-1.jpg', 'box', 'box/letter-2.jpg', 'box/.DS_Store'])
  assert.equal(deep.value.truncated, false)
  const capped = await run(provider, { operation: 'entries', folderId: folder.id, recursive: true }, { max_entries: 2 })
  assert.equal(capped.value.entries.length, 2)
  assert.equal(capped.value.truncated, true)

  const read = await run(provider, { operation: 'read', folderId: folder.id, path: 'box/letter-2.jpg' })
  assert.equal(read.value.name, 'letter-2.jpg')
  assert.equal(read.value.type, 'image/jpeg')
  assert.deepEqual([...new Uint8Array(read.value.bytes)], [4, 5])
  assert.deepEqual(read.transfer, [read.value.bytes])
})

test('paths cannot climb out of the connected folder', async () => {
  assert.throws(() => pathSegments('../secret.txt'), TypeError)
  assert.throws(() => pathSegments('box/../../x'), TypeError)
  assert.throws(() => pathSegments('a\\b'), TypeError)
  assert.deepEqual(pathSegments('/box//letter-2.jpg'), ['box', 'letter-2.jpg'])
})

test('files over the reviewed size limit are refused', async () => {
  const big = new Uint8Array(2 * 1024 * 1024)
  const { provider } = setup({ dialogs: [{ pick: true }], tree: { 'big.pdf': big } })
  const { value: folder } = await run(provider, { operation: 'connect' })
  await assert.rejects(
    run(provider, { operation: 'read', folderId: folder.id, path: 'big.pdf' }, { max_file_bytes: 1024 * 1024 }),
    { code: 'limit_exceeded' },
  )
})

test('after a browser restart the owner allows the folder again before any read', async () => {
  const { provider, root, asked } = setup({ dialogs: [{ pick: true }, { allow: true }] })
  const { value: folder } = await run(provider, { operation: 'connect' })
  root.state.permission = 'prompt'
  assert.equal((await run(provider, { operation: 'list' })).value.folders[0].ready, false)
  const read = await run(provider, { operation: 'read', folderId: folder.id, path: 'letter-1.jpg' })
  assert.equal(read.value.size, 3)
  assert.equal(asked[1].kind, 'reconnect')
  assert.equal(asked[1].folderName, 'Scans')
  assert.equal(root.state.asked, 1)
})

test('declining the re-permission dialog reads nothing', async () => {
  const { provider, root } = setup({ dialogs: [{ pick: true }, null] })
  const { value: folder } = await run(provider, { operation: 'connect' })
  root.state.permission = 'prompt'
  await assert.rejects(run(provider, { operation: 'read', folderId: folder.id, path: 'letter-1.jpg' }), { code: 'aborted' })
  assert.equal(root.state.asked, 0)
})

test('forgetting removes only this app; the last app removes the folder', async () => {
  const { provider, store } = setup({ dialogs: [{ pick: true }] })
  const { value: folder } = await run(provider, { operation: 'connect' })
  store.rows.set(folder.id, { ...store.rows.get(folder.id), apps: ['10', '9'] })
  await run(provider, { operation: 'forget', folderId: folder.id })
  assert.deepEqual(store.rows.get(folder.id).apps, ['9'])
  const other = createFoldersProvider({ appId: 9, store, win: {}, requestDialog: async () => null })
  await run(other, { operation: 'forget', folderId: folder.id })
  assert.equal(store.rows.size, 0)
})

test('browsers without folder access say so instead of opening a dialog', async () => {
  const store = memoryStore()
  let asked = 0
  const provider = createFoldersProvider({ appId: 1, store, win: {}, requestDialog: async () => { asked += 1 } })
  assert.equal((await run(provider, { operation: 'list' })).value.supported, false)
  await assert.rejects(run(provider, { operation: 'connect' }), { code: 'unavailable' })
  assert.equal(asked, 0)
})
