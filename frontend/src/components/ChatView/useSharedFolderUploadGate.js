/* Hold a big chat upload until the owner chooses: share the folder, or upload anyway. */
import { useCallback, useEffect, useState } from 'react'
import { holdUpload } from '../../lib/sharedFolders/uploadSuggestion.js'

export default function useSharedFolderUploadGate({ chatId, addFiles, appendToComposer }) {
  const [pending, setPending] = useState(null)
  useEffect(() => { setPending(null) }, [chatId])

  // Nothing is sent before the owner chooses, so sharing really saves the space.
  const gatedAddFiles = useCallback((fileList) => {
    const files = [...(fileList || [])]
    const held = holdUpload(pending, files)
    if (!held) return addFiles(files)
    setPending(held)
    return Promise.resolve()
  }, [addFiles, pending])

  const uploadAnyway = useCallback(() => {
    const files = pending?.files || []
    setPending(null)
    if (files.length) void addFiles(files)
  }, [pending, addFiles])

  const cancel = useCallback(() => setPending(null), [])

  const folderShared = useCallback((name) => {
    setPending(null)
    appendToComposer(`Files are in my shared folder “${name}”.`)
  }, [appendToComposer])

  return { addFiles: gatedAddFiles, pending, uploadAnyway, cancel, folderShared }
}
