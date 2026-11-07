/* An in-memory stand-in for the Möbius phone app's MobiusNative folder bridge, for tests. */

const MAX_CHUNK = 4 * 1024 * 1024

function failure(code, message = code) {
  return { error: { code, message } }
}

function toBase64(bytes) {
  let binary = ''
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000))
  }
  return btoa(binary)
}

/**
 * Folders are nested plain objects; a file is { bytes, type?, modified? }.
 * `revoke(id)` makes the app lose a folder, as when the owner moves it away;
 * `deleteFolder(id)` removes the folder itself, so the app finds nothing there.
 */
export class FakeNativeBridge {
  constructor() {
    this.folders = new Map()
    this.revoked = new Set()
    this.forgotten = []
    this.reads = []
    this.nextPick = null
    this.count = 0
    // Most bytes one read returns, to act like a provider that answers in smaller pieces.
    this.chunkLimit = MAX_CHUNK
    // Whether a folder's provider lets the app change it, as the app reports on picking.
    this.writable = true
  }

  addFolder(name, tree, mode = 'edit') {
    this.count += 1
    const id = `native-${this.count}`
    this.folders.set(id, { name, tree, mode })
    return id
  }

  /** The folder the next pickFolder() returns; null means the owner cancels. */
  willPick(name, tree) {
    this.nextPick = tree === null ? null : { name, tree }
  }

  revoke(id) {
    this.revoked.add(id)
  }

  async pickFolder({ mode } = {}) {
    if (!this.nextPick) return null
    const { name, tree } = this.nextPick
    this.nextPick = null
    const given = mode === 'edit' && this.writable ? 'edit' : 'read'
    return { id: this.addFolder(name, tree, given), name, mode: given }
  }

  async forgetFolder(id) {
    this.forgotten.push(id)
    this.folders.delete(id)
    return null
  }

  deleteFolder(id) {
    this.folders.delete(id)
  }

  resolve(id, path) {
    if (this.revoked.has(id)) return failure('needs_permission', 'Pick the folder again.')
    const folder = this.folders.get(id)
    if (!folder) return failure('not_found')
    if (path === '') return { name: folder.name, node: folder.tree }
    const segments = path.split('/')
    if (path.startsWith('/') || path.endsWith('/') || segments.some(s => !s || s === '.' || s === '..')) {
      return failure('not_found')
    }
    let node = folder.tree
    for (const segment of segments) {
      if (isFile(node) || !Object.hasOwn(node, segment)) return failure('not_found')
      node = node[segment]
    }
    return { name: segments.at(-1), node }
  }

  async listFolder(id, path) {
    const found = this.resolve(id, path)
    if (found.error) return found
    if (isFile(found.node)) return failure('wrong_kind')
    return { entries: Object.entries(found.node).map(([name, node]) => describe(name, node)) }
  }

  async folderEntry(id, path) {
    const found = this.resolve(id, path)
    return found.error ? found : describe(found.name, found.node)
  }

  /** The folder `path` is in, and its name, for a change; or the failure. */
  changeAt(id, path) {
    if (this.folders.get(id)?.mode !== 'edit') return failure('read_only')
    const slash = path.lastIndexOf('/')
    const parent = this.resolve(id, slash < 0 ? '' : path.slice(0, slash))
    if (parent.error) return parent
    if (isFile(parent.node)) return failure('wrong_kind')
    return { folder: parent.node, name: path.slice(slash + 1) }
  }

  async writeFolderFile(id, path, data, { overwrite, expected } = {}) {
    const at = this.changeAt(id, path)
    if (at.error) return at
    const old = at.folder[at.name]
    if (old && !isFile(old)) return failure('wrong_kind')
    if (old && !overwrite) return failure('exists')
    if (old && expected) {
      const now = describe(at.name, old)
      if (now.size !== expected.size || now.modified !== expected.modified) return failure('changed_during_write')
    }
    const bytes = Uint8Array.from(atob(data), char => char.charCodeAt(0))
    at.folder[at.name] = { bytes }
    return { size: bytes.length, created: !old }
  }

  async makeFolder(id, path) {
    const at = this.changeAt(id, path)
    if (at.error) return at
    const old = at.folder[at.name]
    if (old && isFile(old)) return failure('wrong_kind')
    if (!old) at.folder[at.name] = {}
    return { created: !old }
  }

  async copyFolderFile(id, path, to) {
    return this.transfer(id, path, to, false)
  }

  async moveFolderFile(id, path, to) {
    return this.transfer(id, path, to, true)
  }

  transfer(id, path, to, move) {
    const from = this.changeAt(id, path)
    if (from.error) return from
    const file = from.folder[from.name]
    if (!file) return failure('not_found')
    if (!isFile(file)) return failure('wrong_kind')
    const into = this.changeAt(id, to)
    if (into.error) return into
    if (into.folder[into.name]) return failure('exists')
    into.folder[into.name] = { ...file, bytes: file.bytes.slice() }
    if (move) delete from.folder[from.name]
    return null
  }

  async readFolderFile(id, path, offset, length) {
    const found = this.resolve(id, path)
    if (found.error) return found
    if (!isFile(found.node)) return failure('wrong_kind')
    const capped = Math.min(length, this.chunkLimit)
    this.reads.push({ path, offset, length: capped })
    const { bytes } = found.node
    return { size: bytes.length, data: toBase64(bytes.subarray(offset, offset + capped)) }
  }
}

function isFile(node) {
  return node?.bytes instanceof Uint8Array
}

function describe(name, node) {
  if (!isFile(node)) return { name, kind: 'folder' }
  return { name, kind: 'file', size: 'size' in node ? node.size : node.bytes.length, modified: node.modified ?? 1, type: node.type ?? null }
}

export function textFile(text, extra = {}) {
  return { bytes: new TextEncoder().encode(text), ...extra }
}

/** `size` bytes where byte i is i % 251, so any misplaced chunk shows. */
export function patternFile(size, extra = {}) {
  const bytes = new Uint8Array(size)
  for (let index = 0; index < size; index += 1) bytes[index] = index % 251
  return { bytes, ...extra }
}
