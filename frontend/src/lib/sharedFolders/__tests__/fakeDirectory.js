/* An in-memory stand-in for the browser's FileSystemDirectoryHandle, for tests. */

function domError(name, message = name) {
  const error = new Error(message)
  error.name = name
  return error
}

class FakeWritable extends WritableStream {
  constructor(onCommit) {
    const chunks = []
    super({
      write(chunk) { chunks.push(chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk)) },
      close() { onCommit(new Blob(chunks)) },
    })
  }

  async write(data) {
    const writer = this.getWriter()
    await writer.write(data)
    writer.releaseLock()
  }
}

export class FakeFileHandle {
  kind = 'file'

  constructor(name, blob = new Blob([]), { withMove = true } = {}) {
    this.name = name
    this.blob = blob
    this.modified = 1_700_000_000_000
    if (!withMove) this.move = undefined
  }

  async getFile() {
    return new File([this.blob], this.name, { lastModified: this.modified })
  }

  async createWritable() {
    return new FakeWritable(blob => { this.blob = blob })
  }

  async move(directory, name) {
    this.parent.children.delete(this.name)
    this.name = name
    directory.adopt(this)
  }
}

export class FakeDirectoryHandle {
  kind = 'directory'

  constructor(name, { withMove = true, denied = false } = {}) {
    this.name = name
    this.children = new Map()
    this.withMove = withMove
    this.denied = denied
    // What queryPermission answers for each mode, as the browser remembers it.
    this.grants = { read: 'granted', readwrite: 'granted' }
  }

  async isSameEntry(other) { return other === this }
  async queryPermission({ mode = 'read' } = {}) { return this.grants[mode] }

  adopt(child) {
    child.parent = this
    this.children.set(child.name, child)
    return child
  }

  addFile(name, content) {
    return this.adopt(new FakeFileHandle(name, new Blob([content]), { withMove: this.withMove }))
  }

  addFolder(name) {
    return this.adopt(new FakeDirectoryHandle(name, { withMove: this.withMove }))
  }

  child(name, kind, create) {
    if (this.denied) throw domError('NotAllowedError')
    const found = this.children.get(name)
    if (found && found.kind !== kind) throw domError('TypeMismatchError')
    if (found) return found
    if (!create) throw domError('NotFoundError')
    return kind === 'file'
      ? this.adopt(new FakeFileHandle(name, new Blob([]), { withMove: this.withMove }))
      : this.addFolder(name)
  }

  async getFileHandle(name, { create = false } = {}) { return this.child(name, 'file', create) }
  async getDirectoryHandle(name, { create = false } = {}) { return this.child(name, 'directory', create) }

  async removeEntry(name) {
    if (!this.children.delete(name)) throw domError('NotFoundError')
  }

  async* entries() { for (const entry of this.children) yield entry }
  async* keys() { for (const name of this.children.keys()) yield name }
}

export async function fileText(directory, ...names) {
  let node = directory
  for (const name of names) node = node.children.get(name)
  return (await node.getFile()).text()
}
