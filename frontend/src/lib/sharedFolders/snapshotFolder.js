/* Read-only commands on a folder picked for one visit (Safari, Firefox, Brave): a list of File objects. */
import { FolderError, joinPath, pathSegments } from './folderPaths.js'
import {
  DEFAULT_IMAGE_EDGE, fileInfo, pageOf, readFileSlice, shrinkImage, sortEntries,
} from './fileReads.js'

function readOnlyError() {
  return new FolderError(
    'read_only',
    'This browser shares folders read-only. Changing files needs Chrome or Edge.',
  )
}

function addChild(children, parentPath, child) {
  if (!children.has(parentPath)) children.set(parentPath, new Map())
  children.get(parentPath).set(child.name, child)
}

/** Index a picked folder's files by their path inside it. */
function indexFiles(files) {
  const filesByPath = new Map()
  const children = new Map([['', new Map()]])
  let rootName = ''
  for (const file of files) {
    const parts = (file.webkitRelativePath || file.name).split('/').filter(Boolean)
    if (parts.length > 1 && !rootName) rootName = parts[0]
    const inner = parts.length > 1 ? parts.slice(1) : parts
    filesByPath.set(inner.join('/'), file)
    inner.forEach((name, index) => {
      const parentPath = inner.slice(0, index).join('/')
      const isFile = index === inner.length - 1
      addChild(children, parentPath, isFile ? { name, kind: 'file', file } : { name, kind: 'folder' })
    })
  }
  return { filesByPath, children, rootName }
}

function describe(entry) {
  if (entry.kind === 'folder') return { name: entry.name, kind: 'folder' }
  return { name: entry.name, kind: 'file', size: entry.file.size, modified: entry.file.lastModified || null }
}

function fileAt(index, segments) {
  const path = joinPath(segments)
  const file = index.filesByPath.get(path)
  if (file) return file
  if (index.children.has(path)) throw new FolderError('wrong_kind', `"${path}" is a folder. Use list.`)
  throw new FolderError('not_found', `Nothing is at "${path}" in this folder.`)
}

function folderAt(index, segments) {
  const path = joinPath(segments)
  const entries = index.children.get(path)
  if (entries) return entries
  if (index.filesByPath.has(path)) throw new FolderError('wrong_kind', `"${path}" is a file. Use read or info.`)
  throw new FolderError('not_found', `Nothing is at "${path}" in this folder.`)
}

const OPERATIONS = {
  list(index, command) {
    const segments = pathSegments(command.path, { allowRoot: true })
    const { page, total, nextCursor } = pageOf(sortEntries([...folderAt(index, segments).values()]), command)
    return { path: joinPath(segments), entries: page.map(describe), total, nextCursor }
  },
  info(index, command) {
    const segments = pathSegments(command.path, { allowRoot: true })
    if (!index.children.has(joinPath(segments))) return fileInfo(fileAt(index, segments), joinPath(segments))
    const name = segments.at(-1) || index.rootName
    return { path: joinPath(segments), name, kind: 'folder', entries: folderAt(index, segments).size }
  },
  read(index, command) {
    const segments = pathSegments(command.path)
    return readFileSlice(fileAt(index, segments), joinPath(segments), command)
  },
  image(index, command) {
    const segments = pathSegments(command.path)
    return shrinkImage(fileAt(index, segments), joinPath(segments), command.maxEdge || DEFAULT_IMAGE_EDGE)
  },
}

/** A one-visit folder: list, info and read only, from the files the browser handed over. */
export function createSnapshotFolder(files) {
  const index = indexFiles(files)
  return {
    rootName: index.rootName,
    fileCount: index.filesByPath.size,
    async run(command) {
      const operation = OPERATIONS[command.op]
      if (!operation) throw readOnlyError()
      return operation(index, command)
    },
  }
}
