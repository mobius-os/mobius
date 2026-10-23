// `device.folders`: folders on this computer that the owner connects in the
// shell. The opaque app frame can neither show a directory picker nor keep a
// FileSystemDirectoryHandle, so the shell owns both. Handles live in this
// origin's IndexedDB; each record lists the exact app ids the owner approved
// for it. Apps only ever receive folder ids, relative paths, metadata, and file
// bytes — never a handle.
//
// Every grant and every browser re-permission happens inside a shell dialog the
// owner clicks (requestDialog), so the user activation that the File System
// Access API requires is the shell's own, not the app's.

export const DEVICE_FOLDERS = 'device.folders'

const DB_NAME = 'mobius-local-folders'
const STORE = 'folders'
const DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
const DEFAULT_MAX_ENTRIES = 5000

function folderError(name, message, code) {
  const error = new Error(message)
  error.name = name
  error.code = code
  return error
}

export function foldersSupported(win = globalThis.window) {
  return typeof win?.showDirectoryPicker === 'function'
}

function openDb(idb = globalThis.indexedDB) {
  return new Promise((resolve, reject) => {
    if (!idb) {
      reject(folderError('NotSupportedError', 'This browser cannot remember folders.', 'unavailable'))
      return
    }
    const req = idb.open(DB_NAME, 1)
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE, { keyPath: 'id' })
      }
    }
    req.onsuccess = () => {
      const db = req.result
      // Never block a logout-time deleteDatabase().
      db.onversionchange = () => db.close()
      resolve(db)
    }
    req.onerror = () => reject(req.error)
  })
}

async function withStore(mode, fn, idb) {
  const db = await openDb(idb)
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode)
      const store = tx.objectStore(STORE)
      let value
      Promise.resolve(fn(store)).then((v) => { value = v }, reject)
      tx.oncomplete = () => resolve(value)
      tx.onerror = () => reject(tx.error)
      tx.onabort = () => reject(tx.error)
    })
  } finally {
    db.close()
  }
}

const request = (req) => new Promise((resolve, reject) => {
  req.onsuccess = () => resolve(req.result)
  req.onerror = () => reject(req.error)
})

export function createFolderStore({ idb } = {}) {
  return {
    all: () => withStore('readonly', (s) => request(s.getAll()), idb),
    get: (id) => withStore('readonly', (s) => request(s.get(id)), idb),
    put: (record) => withStore('readwrite', (s) => request(s.put(record)), idb),
    remove: (id) => withStore('readwrite', (s) => request(s.delete(id)), idb),
  }
}

function newFolderId() {
  return globalThis.crypto?.randomUUID?.()
    || `folder-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

// A relative path inside a connected folder: plain names joined by "/".
export function pathSegments(path) {
  if (path == null || path === '') return []
  if (typeof path !== 'string' || path.length > 4096) {
    throw new TypeError('Folder paths must be text.')
  }
  const parts = path.split('/').filter((part) => part !== '')
  for (const part of parts) {
    if (part === '.' || part === '..' || part.includes('\\') || part.includes('\0')) {
      throw new TypeError('Folder paths may only name files and folders inside the connected folder.')
    }
  }
  return parts
}

function notFound(path) {
  return folderError('NotFoundError', `Nothing called "${path}" in this folder.`, 'not_found')
}

async function resolveDirectory(root, parts, fullPath) {
  let dir = root
  for (const part of parts) {
    try {
      dir = await dir.getDirectoryHandle(part)
    } catch {
      throw notFound(fullPath)
    }
  }
  return dir
}

async function resolveFile(root, path) {
  const parts = pathSegments(path)
  if (!parts.length) throw new TypeError('Name a file to read.')
  const dir = await resolveDirectory(root, parts.slice(0, -1), path)
  try {
    return await dir.getFileHandle(parts[parts.length - 1])
  } catch {
    throw notFound(path)
  }
}

async function listEntries(root, path, { recursive, maxEntries }) {
  const base = pathSegments(path)
  const start = await resolveDirectory(root, base, path)
  const out = []
  let truncated = false
  const queue = [[start, base.join('/')]]
  while (queue.length) {
    const [dir, prefix] = queue.shift()
    for await (const [name, handle] of dir.entries()) {
      if (out.length >= maxEntries) { truncated = true; break }
      const entryPath = prefix ? `${prefix}/${name}` : name
      if (handle.kind === 'directory') {
        out.push({ path: entryPath, name, kind: 'directory' })
        if (recursive) queue.push([handle, entryPath])
      } else {
        let size = null
        let modified = null
        try {
          const file = await handle.getFile()
          size = file.size
          modified = file.lastModified
        } catch { /* unreadable entry: still list it */ }
        out.push({ path: entryPath, name, kind: 'file', size, modified })
      }
    }
    if (truncated) break
  }
  return { entries: out, truncated }
}

async function permissionOf(handle) {
  try {
    return await handle.queryPermission({ mode: 'read' })
  } catch {
    return 'prompt'
  }
}

export function createFoldersProvider({
  appId,
  getAppName = () => '',
  requestDialog,
  store = createFolderStore(),
  win = globalThis.window,
} = {}) {
  const app = String(appId || '')

  async function grantedRecord(folderId) {
    if (typeof folderId !== 'string' || !folderId) throw new TypeError('Name a connected folder.')
    const record = await store.get(folderId)
    if (!record || !record.apps?.includes(app)) {
      throw folderError('NotAllowedError', 'This folder is not connected to this app.', 'denied')
    }
    return record
  }

  // Browsers drop a remembered folder's permission after a restart; asking
  // again needs a click, which the shell dialog supplies.
  // `clicked` means the owner has just pressed a shell dialog button, so the
  // browser prompt can run on that same click without a second dialog.
  async function readable(record, { clicked = false } = {}) {
    if (await permissionOf(record.handle) === 'granted') return record.handle
    if (!clicked) {
      const choice = await requestDialog({
        kind: 'reconnect', appName: getAppName(), folderName: record.name,
      })
      if (!choice?.allow) {
        throw folderError('AbortError', 'Folder access was not allowed.', 'aborted')
      }
    }
    let state = 'denied'
    try { state = await record.handle.requestPermission({ mode: 'read' }) } catch {}
    if (state !== 'granted') {
      throw folderError('NotAllowedError', 'The browser did not allow access to this folder.', 'denied')
    }
    return record.handle
  }

  async function connect() {
    if (!foldersSupported(win)) {
      throw folderError(
        'NotSupportedError',
        'This browser cannot connect folders. Use Chrome or Edge on a computer.',
        'unavailable',
      )
    }
    const known = await store.all()
    const choice = await requestDialog({
      kind: 'connect',
      appName: getAppName(),
      folders: known.map((r) => ({ id: r.id, name: r.name, connected: r.apps?.includes(app) })),
    })
    if (!choice) throw folderError('AbortError', 'No folder was connected.', 'aborted')

    let record
    if (choice.folderId) {
      record = known.find((r) => r.id === choice.folderId)
      if (!record) throw notFound(choice.folderId)
      await readable(record, { clicked: true })
    } else {
      let handle
      try {
        handle = await win.showDirectoryPicker({ id: 'mobius-folders', mode: 'read' })
      } catch (error) {
        if (error?.name === 'AbortError') {
          throw folderError('AbortError', 'No folder was chosen.', 'aborted')
        }
        throw folderError('NotAllowedError', error?.message || 'The folder could not be opened.', 'denied')
      }
      for (const existing of known) {
        try {
          if (await existing.handle.isSameEntry(handle)) { record = existing; break }
        } catch { /* a stale handle is simply not the same folder */ }
      }
      if (record) record = { ...record, handle }
      else record = { id: newFolderId(), name: handle.name, handle, apps: [], addedAt: Date.now() }
    }
    if (!record.apps.includes(app)) record = { ...record, apps: [...record.apps, app] }
    await store.put(record)
    return { id: record.id, name: record.name }
  }

  return {
    version: 1,
    // One owner dialog at a time per app; reads and listings run side by side.
    contention({ input, activeInput }) {
      return input?.operation === 'connect' && activeInput?.operation === 'connect'
        ? 'replace'
        : 'share'
    },
    async open({ input, declaration, channel }) {
      const operation = input?.operation
      const limits = declaration?.limits || {}
      if (operation === 'list') {
        const records = (await store.all().catch(() => [])).filter((r) => r.apps?.includes(app))
        const folders = []
        for (const r of records) {
          folders.push({ id: r.id, name: r.name, ready: await permissionOf(r.handle) === 'granted' })
        }
        channel.result({ supported: foldersSupported(win), folders })
        return {}
      }
      if (operation === 'connect') {
        channel.result(await connect())
        return {}
      }
      if (operation === 'forget') {
        const record = await grantedRecord(input.folderId)
        const apps = record.apps.filter((id) => id !== app)
        if (apps.length) await store.put({ ...record, apps })
        else await store.remove(record.id)
        channel.result({ forgotten: true })
        return {}
      }
      if (operation === 'entries') {
        const root = await readable(await grantedRecord(input.folderId))
        const cap = Number(limits.max_entries) || DEFAULT_MAX_ENTRIES
        const asked = Number(input.maxEntries)
        const maxEntries = Math.max(1, Math.min(cap, Number.isFinite(asked) && asked > 0 ? asked : cap))
        channel.result(await listEntries(root, input.path || '', {
          recursive: input.recursive === true, maxEntries,
        }))
        return {}
      }
      if (operation === 'read') {
        const root = await readable(await grantedRecord(input.folderId))
        const file = await (await resolveFile(root, input.path)).getFile()
        const maxBytes = Number(limits.max_file_bytes) || DEFAULT_MAX_FILE_BYTES
        if (file.size > maxBytes) {
          throw folderError(
            'QuotaExceededError',
            `${file.name} is larger than this app may read at once (${Math.round(maxBytes / 1048576)} MB).`,
            'limit_exceeded',
          )
        }
        const bytes = await file.arrayBuffer()
        channel.result({
          path: pathSegments(input.path).join('/'),
          name: file.name,
          type: file.type || '',
          size: file.size,
          modified: file.lastModified,
          bytes,
        }, [bytes])
        return {}
      }
      throw new TypeError('Unknown folder operation.')
    },
  }
}
