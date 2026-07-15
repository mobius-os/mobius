// Clipboard-file extraction for chat paste uploads.

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
