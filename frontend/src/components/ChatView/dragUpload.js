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

function claimFileDrag(event) {
  event.preventDefault?.()
  event.stopPropagation?.()
}

/**
 * Own the synchronous enter/leave depth independently of React render timing.
 * Browser drag events can enter and leave nested children before a state update
 * commits, so handlers must read the ref owner rather than a captured boolean.
 */
export function createFileDragHandlers({
  getDepth,
  setDepth,
  setActive,
  onFiles,
}) {
  return {
    onDragEnter(event) {
      if (!dataTransferHasFiles(event.dataTransfer)) return
      claimFileDrag(event)
      setDepth(getDepth() + 1)
      setActive(true)
    },

    onDragOver(event) {
      if (!dataTransferHasFiles(event.dataTransfer)) return
      claimFileDrag(event)
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
    },

    onDragLeave(event) {
      const depth = getDepth()
      if (depth <= 0) return
      claimFileDrag(event)
      const nextDepth = Math.max(0, depth - 1)
      setDepth(nextDepth)
      if (nextDepth === 0) setActive(false)
    },

    onDrop(event) {
      if (!dataTransferHasFiles(event.dataTransfer)) return
      claimFileDrag(event)
      setDepth(0)
      setActive(false)
      const files = droppedFiles(event.dataTransfer)
      if (files.length > 0) onFiles(files)
    },
  }
}
