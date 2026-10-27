/* Commands on a folder the Möbius phone app keeps, through its MobiusNative bridge. */
import {
  BIN_FOLDER_NAME, FolderError, isInBin, joinPath, numberedName, pathSegments,
} from './folderPaths.js'
import {
  DEFAULT_IMAGE_EDGE, base64ToBytes, bytesToBase64, fileInfo, pageOf, readFileSlice, shrinkImage, sortEntries,
} from './fileReads.js'
import { ERROR_MESSAGES, MAX_WRITE_BYTES, changedDuringWrite, editableSegments } from './handleFolder.js'

// The bridge returns at most this much per readFolderFile call.
export const NATIVE_CHUNK_BYTES = 4 * 1024 * 1024
// An image is decoded whole; past this a phone WebView risks running out of memory.
const MAX_IMAGE_SOURCE_BYTES = 64 * 1024 * 1024
const BRIDGE_CODES = new Set([
  'not_found', 'wrong_kind', 'needs_permission',
  'exists', 'read_only', 'no_space', 'too_large', 'changed_during_move', 'changed_during_write', 'original_kept',
])
const EDIT_OPS = new Set(['write', 'move', 'mkdir', 'bin'])

export const NATIVE_NEEDS_PICK_MESSAGE = 'The Möbius app can no longer open this folder. Ask the owner to tap Pick again for it in Settings → Shared folders.'

/**
 * The phone app's folder bridge, or null. Older app builds have MobiusNative
 * without the folder calls, so the picker itself is what is detected.
 */
export function nativeFolderBridge(native = globalThis.MobiusNative) {
  return typeof native?.pickFolder === 'function' ? native : null
}

/** Whether the phone app can change folders too (older builds only read them). */
export function nativeFolderEditing(native = globalThis.MobiusNative) {
  return typeof native?.writeFolderFile === 'function'
}

/** A bridge failure ({ error: { code, message } }) as the folder error agents already know. */
export function nativeFolderError(error, path = '') {
  const code = BRIDGE_CODES.has(error?.code) ? error.code : 'device_error'
  if (code === 'needs_permission') return new FolderError(code, NATIVE_NEEDS_PICK_MESSAGE)
  const known = code === 'device_error' ? null : ERROR_MESSAGES[code]?.(path)
  const shown = known || String(error?.message || 'The Möbius app could not open this file.').slice(0, 300)
  return new FolderError(code, shown)
}

function decodeBase64(data) {
  if (typeof Uint8Array.fromBase64 === 'function') return Uint8Array.fromBase64(data)
  const binary = atob(data)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return bytes
}

function concatBytes(parts, total) {
  if (parts.length === 1) return parts[0]
  const bytes = new Uint8Array(total)
  let at = 0
  for (const part of parts) {
    bytes.set(part, at)
    at += part.length
  }
  return bytes
}

/** One native folder, by the opaque id the app keeps it under. */
function bridgeFolder(bridge, nativeId) {
  async function call(method, path, ...args) {
    const result = await bridge[method](nativeId, path, ...args)
    if (result?.error) throw nativeFolderError(result.error, path)
    return result
  }

  /**
   * Bytes [start, end) of a file, fetched in bridge-sized chunks. A short
   * chunk is not taken as the end: reading goes on until `end` or the file's
   * own size, so a provider that returns less than asked never truncates a
   * read silently. An empty chunk ends it (the file shrank), so it cannot loop.
   */
  async function readRange(path, start, end) {
    const parts = []
    let offset = start
    let total = 0
    while (offset < end) {
      const length = Math.min(NATIVE_CHUNK_BYTES, end - offset)
      const { size, data } = await call('readFolderFile', path, offset, length)
      const bytes = decodeBase64(data || '')
      if (!bytes.length) break
      parts.push(bytes)
      total += bytes.length
      offset += bytes.length
      if (typeof size === 'number' && offset >= size) break
    }
    return concatBytes(parts, total)
  }

  /**
   * The few File members the shared readers use (name, size, type,
   * lastModified, slice().arrayBuffer()), reading lazily through the bridge.
   */
  async function lazyFile(path, entry) {
    let size = entry.size
    if (typeof size !== 'number') size = (await call('readFolderFile', path, 0, 1)).size ?? 0
    return {
      name: entry.name,
      size,
      type: entry.type || '',
      lastModified: entry.modified || 0,
      slice: (start = 0, end = size) => ({
        arrayBuffer: async () => (await readRange(path, start, Math.min(end, size))).slice().buffer,
      }),
    }
  }

  /** A real File, for decoders that need the whole blob (images). */
  async function wholeFile(path, entry) {
    const file = await lazyFile(path, entry)
    if (file.size > MAX_IMAGE_SOURCE_BYTES) {
      throw new FolderError('too_large', 'This image is over 64 MB, too big to open on the phone. Read it with --base64 in parts instead.')
    }
    const bytes = await readRange(path, 0, file.size)
    return new File([bytes], file.name, { type: file.type, lastModified: file.lastModified })
  }

  async function fileEntry(path) {
    const entry = await call('folderEntry', path)
    if (entry.kind !== 'file') throw new FolderError('wrong_kind', `"${path}" is a folder. Use list.`)
    return entry
  }

  /** The entry at `path`, or null if nothing is there. */
  async function entryOrNull(path) {
    try {
      return await call('folderEntry', path)
    } catch (error) {
      if (error?.code === 'not_found') return null
      throw error
    }
  }

  return { call, lazyFile, wholeFile, fileEntry, entryOrNull }
}

function describe(entry) {
  if (entry.kind === 'folder') return { name: entry.name, kind: 'folder' }
  return { name: entry.name, kind: 'file', size: entry.size ?? null, modified: entry.modified || null }
}

const OPERATIONS = {
  async list(folder, command) {
    const path = joinPath(pathSegments(command.path, { allowRoot: true }))
    const { entries } = await folder.call('listFolder', path)
    const { page, total, nextCursor } = pageOf(sortEntries(entries || []), command)
    return { path, entries: page.map(describe), total, nextCursor }
  },
  async info(folder, command) {
    const path = joinPath(pathSegments(command.path, { allowRoot: true }))
    const entry = await folder.call('folderEntry', path)
    if (entry.kind === 'file') return fileInfo(await folder.lazyFile(path, entry), path)
    const { entries } = await folder.call('listFolder', path)
    return { path, name: entry.name, kind: 'folder', entries: (entries || []).length }
  },
  async read(folder, command) {
    const path = joinPath(pathSegments(command.path))
    return readFileSlice(await folder.lazyFile(path, await folder.fileEntry(path)), path, command)
  },
  async image(folder, command) {
    const path = joinPath(pathSegments(command.path))
    const file = await folder.wholeFile(path, await folder.fileEntry(path))
    return shrinkImage(file, path, command.maxEdge || DEFAULT_IMAGE_EDGE)
  },
}

/** A free name for `name` in the Möbius bin. */
async function freeBinName(folder, name) {
  if (!(await folder.entryOrNull(`${BIN_FOLDER_NAME}/${name}`))) return name
  for (let number = 2; number < 1000; number += 1) {
    const candidate = numberedName(name, number)
    if (!(await folder.entryOrNull(`${BIN_FOLDER_NAME}/${candidate}`))) return candidate
  }
  throw new FolderError('exists', 'The Möbius bin has too many files with this name. Ask the owner to empty it.')
}

/** Copies the file at `path` into the Möbius bin; its place there. */
async function copyToBin(folder, path, name) {
  await folder.call('makeFolder', BIN_FOLDER_NAME)
  const binned = `${BIN_FOLDER_NAME}/${await freeBinName(folder, name)}`
  await folder.call('copyFolderFile', path, binned)
  return binned
}

/** The file to move, as the app moves files but not whole folders. */
async function movableFile(folder, path) {
  const entry = await folder.call('folderEntry', path)
  if (entry.kind === 'folder') {
    throw new FolderError(
      'folder_move_unsupported',
      'The Möbius app can move files but not whole folders. Move the files inside it one by one.',
    )
  }
  return entry
}

/** The same rules as browser folders (handleFolder.js): never in the bin, never hidden names, nothing deleted. */
const EDITS = {
  async write(folder, command) {
    const segments = editableSegments(command.path)
    const path = joinPath(segments)
    const bytes = command.encoding === 'base64'
      ? base64ToBytes(command.content)
      : new TextEncoder().encode(command.content)
    if (bytes.length > MAX_WRITE_BYTES) throw new FolderError('too_large', 'Write at most 4 MB at a time.')
    const old = await folder.entryOrNull(path)
    if (old?.kind === 'folder') throw new FolderError('wrong_kind', `"${path}" is a folder.`)
    if (old && !command.overwrite) {
      throw new FolderError('exists', `"${path}" already exists. Use --overwrite to replace it.`)
    }
    // Replacing keeps the old version: a copy goes to the bin before anything is written,
    // and only that same version is replaced (a newer one is not, as the bin lacks it).
    const binned = old ? await copyToBin(folder, path, segments.at(-1)) : null
    if (old) {
      const current = await folder.entryOrNull(path)
      if (current?.modified !== old.modified || current?.size !== old.size) throw changedDuringWrite(path, binned)
    }
    // Canonical base64 of the checked bytes, whatever spacing the agent's text had. An app that
    // knows `expected` also refuses itself if the file changed after the check above.
    const options = old
      ? { overwrite: true, expected: { size: old.size ?? null, modified: old.modified ?? null } }
      : { overwrite: false }
    try {
      await folder.call('writeFolderFile', path, bytesToBase64(bytes), options)
    } catch (error) {
      throw error?.code === 'changed_during_write' ? changedDuringWrite(path, binned) : error
    }
    return { path, size: bytes.length, created: !old, ...(binned && { binned }) }
  },
  async move(folder, command) {
    const from = joinPath(editableSegments(command.path))
    const to = joinPath(editableSegments(command.to))
    await movableFile(folder, from)
    if (await folder.entryOrNull(to)) throw new FolderError('exists', `"${to}" already exists. Choose another name.`)
    await folder.call('moveFolderFile', from, to)
    return { path: from, to }
  },
  async mkdir(folder, command) {
    const path = joinPath(editableSegments(command.path))
    const { created } = await folder.call('makeFolder', path)
    return { path, created: Boolean(created) }
  },
  async bin(folder, command) {
    if (isInBin(pathSegments(command.path))) {
      throw new FolderError('already_in_bin', 'This file is already in the Möbius bin. Only the owner empties it.')
    }
    const segments = editableSegments(command.path)
    const path = joinPath(segments)
    await movableFile(folder, path)
    await folder.call('makeFolder', BIN_FOLDER_NAME)
    const to = `${BIN_FOLDER_NAME}/${await freeBinName(folder, segments.at(-1))}`
    await folder.call('moveFolderFile', path, to)
    return { path, to }
  },
}

/**
 * A folder kept by the phone app. Changes need an app build with the folder
 * writes, and a folder picked with "edit"; the app refuses them otherwise too.
 */
export function createNativeFolder(bridge, nativeId) {
  const folder = bridgeFolder(bridge, nativeId)
  const editing = nativeFolderEditing(bridge)
  return {
    async run(command) {
      if (EDIT_OPS.has(command.op) && !editing) {
        throw new FolderError('read_only', 'Folders shared from this version of the Möbius app are read only.')
      }
      const operation = OPERATIONS[command.op] || EDITS[command.op]
      if (!operation) throw new FolderError('invalid_request', `Unknown folder operation "${command.op}".`)
      return operation(folder, command)
    },
  }
}

/**
 * Whether the app can still open the folder, without asking the owner anything.
 * Only the folder's own entry counts as access: a folder deleted on the phone
 * answers not_found, and that or any other failure the app reports needs Pick again.
 */
export async function nativeFolderPermission(bridge, nativeId) {
  if (!bridge) return 'denied'
  try {
    const result = await bridge.folderEntry(nativeId, '')
    return result && !result.error ? 'granted' : 'denied'
  } catch {
    // The bridge itself failed; the next command reports what is wrong.
    return 'granted'
  }
}
