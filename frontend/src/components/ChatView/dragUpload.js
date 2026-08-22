/** Whether a browser drag payload carries one or more local files. */
export function dataTransferHasFiles(dataTransfer) {
  if (!dataTransfer) return false
  if (Array.from(dataTransfer.types || []).includes('Files')) return true
  if (Array.from(dataTransfer.files || []).some(Boolean)) return true
  return Array.from(dataTransfer.items || []).some(item => item?.kind === 'file')
}

/** Return the real File objects exposed once a local-file drop completes. */
export function droppedFiles(dataTransfer) {
  if (!dataTransfer) return []
  return Array.from(dataTransfer.files || []).filter(Boolean)
}
