// Normalize browser clipboard payloads into the composer's text + file model.

const CLIPBOARD_MIME_EXTENSIONS = new Map([
  ['application/json', 'json'],
  ['application/pdf', 'pdf'],
  ['application/zip', 'zip'],
  ['audio/mpeg', 'mp3'],
  ['audio/wav', 'wav'],
  ['image/gif', 'gif'],
  ['image/jpeg', 'jpg'],
  ['image/png', 'png'],
  ['image/svg+xml', 'svg'],
  ['image/webp', 'webp'],
  ['video/mp4', 'mp4'],
  ['video/webm', 'webm'],
])

function normalizedMimeType(type) {
  return String(type || '').split(';', 1)[0].trim().toLowerCase()
}

function isAttachableClipboardType(type) {
  const mime = normalizedMimeType(type)
  return mime.includes('/') && !mime.startsWith('text/') && !mime.startsWith('web ')
}

function clipboardFileName(type, index) {
  const mime = normalizedMimeType(type)
  const known = CLIPBOARD_MIME_EXTENSIONS.get(mime)
  const subtype = mime.split('/')[1] || ''
  const derived = subtype
    .replace(/^x-/, '')
    .split('+', 1)[0]
    .replace(/[^a-z0-9]/g, '')
    .slice(0, 12)
  const extension = known || derived || 'bin'
  const kind = mime.startsWith('image/') ? 'image' : 'file'
  const suffix = index > 0 ? `-${index + 1}` : ''
  return `clipboard-${kind}${suffix}.${extension}`
}

function clipboardBlobAsFile(blob, type, index, FileImpl = globalThis.File) {
  if (typeof FileImpl !== 'function') return null
  return new FileImpl(
    [blob],
    clipboardFileName(type || blob?.type, index),
    { type: normalizedMimeType(type || blob?.type) || 'application/octet-stream' },
  )
}

function htmlToPlainText(html, DOMParserImpl = globalThis.DOMParser) {
  if (typeof DOMParserImpl !== 'function') return ''
  const document = new DOMParserImpl().parseFromString(String(html || ''), 'text/html')
  return document?.body?.textContent || ''
}

async function readClipboardItemText(item, types) {
  const type = ['text/plain', 'text/uri-list', 'text/html']
    .find(candidate => types.includes(candidate))
  if (!type) return ''
  const value = await (await item.getType(type)).text()
  return type === 'text/html' ? htmlToPlainText(value) : value
}

function clipboardFailureStatus(error) {
  return ['NotAllowedError', 'SecurityError'].includes(error?.name)
    ? 'denied'
    : 'failed'
}

/** Read every clipboard item the browser exposes, choosing one semantic
 * representation per item: a binary format becomes an attachment; otherwise
 * its plain-text (or text-equivalent) representation becomes composer text.
 * Known browser limitations are explicit outcomes, not thrown control flow. */
export async function readClipboardContents(
  clipboard = globalThis.navigator?.clipboard,
  FileImpl = globalThis.File,
) {
  if (!clipboard) return { status: 'unsupported', files: [], text: '' }

  if (typeof clipboard.read !== 'function') {
    if (typeof clipboard.readText !== 'function') {
      return { status: 'unsupported', files: [], text: '' }
    }
    try {
      const text = await clipboard.readText()
      return text
        ? { status: 'ready', files: [], text }
        : { status: 'empty', files: [], text: '' }
    } catch (error) {
      return { status: clipboardFailureStatus(error), files: [], text: '' }
    }
  }

  let items
  try {
    items = await clipboard.read()
  } catch (error) {
    return { status: clipboardFailureStatus(error), files: [], text: '' }
  }

  const files = []
  const textParts = []
  let itemReadFailed = false
  for (const item of items || []) {
    const types = Array.from(item?.types || []).map(normalizedMimeType)
    const attachmentType = types.find(type => type.startsWith('image/'))
      || types.find(isAttachableClipboardType)
    try {
      if (attachmentType) {
        const blob = await item.getType(attachmentType)
        const file = clipboardBlobAsFile(blob, attachmentType, files.length, FileImpl)
        if (file) files.push(file)
        continue
      }
      const text = await readClipboardItemText(item, types)
      if (text) textParts.push(text)
    } catch {
      itemReadFailed = true
    }
  }

  let text = textParts.join('\n')
  // Some browsers implement rich reads but omit a usable type for otherwise
  // ordinary text. Their narrower readText() path is still worth trying.
  if (files.length === 0 && !text && typeof clipboard.readText === 'function') {
    try { text = await clipboard.readText() } catch {}
  }

  if (files.length > 0 || text) return { status: 'ready', files, text }
  return {
    status: itemReadFailed ? 'failed' : 'empty',
    files: [],
    text: '',
  }
}

/** Insert clipboard text into a controlled textarea without losing the
 * selection that was active when the composer menu opened. */
export function insertClipboardText(value, text, selectionStart, selectionEnd) {
  const source = String(value || '')
  const start = Number.isInteger(selectionStart)
    ? Math.max(0, Math.min(selectionStart, source.length))
    : source.length
  const end = Number.isInteger(selectionEnd)
    ? Math.max(start, Math.min(selectionEnd, source.length))
    : start
  const inserted = String(text || '')
  return {
    value: `${source.slice(0, start)}${inserted}${source.slice(end)}`,
    caret: start + inserted.length,
  }
}

/** Return every real File carried by a paste event's DataTransfer.
 * `clipboardData.files` is the reliable path for screenshots; the item
 * fallback covers browsers that expose pasted images only through items. */
export function pastedFiles(clipboardData) {
  if (!clipboardData) return []
  const direct = Array.from(clipboardData.files || []).filter(Boolean)
  if (direct.length > 0) return direct
  return Array.from(clipboardData.items || [])
    .filter(item => item?.kind === 'file')
    .map(item => item.getAsFile?.())
    .filter(Boolean)
}

/** Preserve a simultaneous text paste, but suppress the browser's empty or
 * replacement-character insertion for a file-only clipboard payload. */
export function filePasteNeedsDefaultPrevented(clipboardData, files) {
  if (!files?.length) return false
  try {
    return !(clipboardData.getData?.('text/plain') || '').trim()
  } catch {
    return true
  }
}
