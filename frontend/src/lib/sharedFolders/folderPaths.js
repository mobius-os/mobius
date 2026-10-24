/* Paths inside a shared folder: plain names only, the same rule the server enforces. */

// Kept in step with backend/app/routes/shared_folders.py normalized_path().
const UNSAFE_SEGMENT = /[\u0000-\u001f\u007f/\\]/
const MAX_PATH_CHARS = 1024
const MAX_PATH_DEPTH = 32

export const BIN_FOLDER_NAME = 'Möbius bin'

export class FolderError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'FolderError'
    this.code = code
  }
}

/** Split a folder-relative path into names; never allows leaving the folder. */
export function pathSegments(path, { allowRoot = false } = {}) {
  const stripped = String(path ?? '').trim().replace(/^\/+|\/+$/g, '')
  if (!stripped) {
    if (allowRoot) return []
    throw new FolderError('invalid_path', 'A file path inside the folder is required.')
  }
  if (stripped.length > MAX_PATH_CHARS) throw new FolderError('invalid_path', 'The path is too long.')
  const segments = stripped.split('/')
  if (segments.length > MAX_PATH_DEPTH) throw new FolderError('invalid_path', 'The path is nested too deeply.')
  const unsafe = segments.some(segment => (
    segment === '' || segment === '.' || segment === '..' || UNSAFE_SEGMENT.test(segment)
  ))
  if (unsafe) {
    throw new FolderError('invalid_path', 'Use plain folder and file names inside the shared folder.')
  }
  return segments
}

// macOS and Windows ignore letter case, and macOS stores "ö" as two code points.
const comparableName = name => name.normalize('NFC').toLowerCase()

/** The Möbius bin, or anything inside it, however the name is spelled. */
export function isInBin(segments) {
  return segments.length > 0 && comparableName(segments[0]) === comparableName(BIN_FOLDER_NAME)
}

export function joinPath(segments) {
  return segments.join('/')
}

/** "photo.jpg" → "photo (2).jpg", for a free name next to an existing one. */
export function numberedName(name, number) {
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return `${name} (${number})`
  return `${name.slice(0, dot)} (${number})${name.slice(dot)}`
}
