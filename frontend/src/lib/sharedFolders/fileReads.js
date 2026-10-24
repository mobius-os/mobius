/* Reading files and listing entries, shared by remembered and one-visit folders. */
import { FolderError } from './folderPaths.js'

export const DEFAULT_READ_BYTES = 256 * 1024
// Kept in step with MAX_READ_BYTES in backend/app/routes/shared_folders.py.
export const MAX_READ_BYTES = 4 * 1024 * 1024
export const DEFAULT_LIST_LIMIT = 200
// Claude's largest image input is 2,576 px on the long edge.
export const DEFAULT_IMAGE_EDGE = 2576
const MAX_LIST_LIMIT = 500

export function fileInfo(file, path) {
  return {
    path,
    name: file.name,
    kind: 'file',
    size: file.size,
    modified: file.lastModified || null,
    type: file.type || null,
  }
}

/** One byte range of a file, as text or base64; never the whole file by default. */
export async function readFileSlice(file, path, options = {}) {
  const length = Math.min(Math.max(1, options.length || DEFAULT_READ_BYTES), MAX_READ_BYTES)
  const start = options.fromEnd
    ? Math.max(0, file.size - length)
    : Math.min(Math.max(0, options.offset || 0), file.size)
  const bytes = new Uint8Array(await file.slice(start, start + length).arrayBuffer())
  if (options.encoding === 'base64') {
    return { ...sliceInfo(file, path, start, bytes.length), base64: bytesToBase64(bytes) }
  }
  if (bytes.includes(0)) {
    throw new FolderError(
      'binary',
      'This looks like a binary file. Use `folders show` for images and PDFs, or read it with --base64.',
    )
  }
  const [from, to] = wholeCharacterRange(bytes, start > 0, start + bytes.length < file.size)
  return {
    ...sliceInfo(file, path, start + from, to - from),
    text: new TextDecoder('utf-8').decode(bytes.subarray(from, to)),
  }
}

function sliceInfo(file, path, offset, length) {
  return { ...fileInfo(file, path), offset, length, eof: offset + length >= file.size }
}

const isContinuationByte = byte => (byte & 0xc0) === 0x80

function utf8SequenceLength(leadByte) {
  if (leadByte >= 0xf0) return 4
  if (leadByte >= 0xe0) return 3
  if (leadByte >= 0xc0) return 2
  return 1
}

/**
 * The part of a text slice that holds whole UTF-8 characters, as [from, to).
 * Page edges then never split a letter such as "č", and the next page's
 * offset starts on a character. A slice too short to hold one is kept as is.
 */
function wholeCharacterRange(bytes, cutAtStart, cutAtEnd) {
  let from = 0
  while (cutAtStart && from < 3 && isContinuationByte(bytes[from])) from += 1
  let to = bytes.length
  if (cutAtEnd) {
    let lead = to - 1
    while (lead > from && to - lead < 4 && isContinuationByte(bytes[lead])) lead -= 1
    if (to - lead < utf8SequenceLength(bytes[lead])) to = lead
  }
  return to > from ? [from, to] : [0, bytes.length]
}

// Formats every model reader accepts as they are; anything else is re-encoded.
const SENDABLE_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/gif', 'image/webp'])
const JPEG_QUALITY = 0.85

async function decodeImage(file) {
  try {
    return await createImageBitmap(file)
  } catch {
    return null
  }
}

async function encodeJpeg(bitmap, width, height) {
  const canvas = typeof OffscreenCanvas === 'function'
    ? new OffscreenCanvas(width, height)
    : Object.assign(document.createElement('canvas'), { width, height })
  const context = canvas.getContext('2d')
  // JPEG has no transparency: paint white first so clear areas stay light.
  context.fillStyle = '#fff'
  context.fillRect(0, 0, width, height)
  context.drawImage(bitmap, 0, 0, width, height)
  if (canvas.convertToBlob) return canvas.convertToBlob({ type: 'image/jpeg', quality: JPEG_QUALITY })
  return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', JPEG_QUALITY))
}

/**
 * A photo the AI can look at, shrunk on this device so a 5 MB photo travels
 * as a few hundred KB. The model scales images down to this size anyway.
 */
export async function shrinkImage(file, path, maxEdge) {
  const bitmap = await decodeImage(file)
  if (!bitmap) {
    throw new FolderError(
      'unsupported_image',
      'This browser cannot open this image type (for example HEIC). Ask the owner to share a JPEG or PNG copy.',
    )
  }
  const { width, height } = bitmap
  const scale = Math.min(1, maxEdge / Math.max(width, height))
  const fits = scale === 1 && SENDABLE_IMAGE_TYPES.has(file.type) && file.size <= MAX_READ_BYTES
  try {
    const blob = fits
      ? file
      : await encodeJpeg(bitmap, Math.round(width * scale), Math.round(height * scale))
    if (blob.size > MAX_READ_BYTES) throw new FolderError('too_large', 'The shrunk image is still over 4 MB.')
    return {
      ...fileInfo(file, path),
      type: fits ? file.type : 'image/jpeg',
      width: Math.round(width * scale),
      height: Math.round(height * scale),
      originalWidth: width,
      originalHeight: height,
      shrunk: !fits,
      bytes: blob.size,
      base64: bytesToBase64(new Uint8Array(await blob.arrayBuffer())),
    }
  } finally {
    bitmap.close?.()
  }
}

export function bytesToBase64(bytes) {
  let binary = ''
  const chunk = 0x8000
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk))
  }
  return btoa(binary)
}

export function base64ToBytes(text) {
  try {
    return Uint8Array.from(atob(text), char => char.charCodeAt(0))
  } catch {
    throw new FolderError('invalid_content', 'The file content is not valid base64.')
  }
}

/** Folders first, then names in a stable, human order. */
export function sortEntries(entries) {
  return [...entries].sort((a, b) => (
    a.kind === b.kind ? a.name.localeCompare(b.name) : (a.kind === 'folder' ? -1 : 1)
  ))
}

export function pageOf(names, { cursor = 0, limit = DEFAULT_LIST_LIMIT } = {}) {
  const size = Math.min(Math.max(1, limit), MAX_LIST_LIMIT)
  const page = names.slice(cursor, cursor + size)
  const next = cursor + page.length
  return { page, total: names.length, nextCursor: next < names.length ? next : null }
}
