import { useState, useRef, useEffect } from 'react'
import { getAuthHeaders, BASE } from '../../api/client.js'

/**
 * Hook encapsulating file upload state and API calls for chat attachments.
 *
 * @param {{ chatId: string, initialFiles?: Array }} options
 * @returns {{
 *   files: Array,
 *   addFiles: (fileList: File[]) => Promise<void>,
 *   removeFile: (id: string) => void,
 *   clearFiles: (opts?: {revoke?: boolean}) => void,
 *   restoreFiles: (files: Array) => void,
 *   releaseFiles: (files: Array) => void,
 * }}
 */
export default function useFileUpload({ chatId, initialFiles = [], onFilesChange }) {
  const normalizedInitialFiles = initialFiles.map((file, index) => ({
    id: file.id || `restored-${index}-${file.name || 'file'}`,
    name: file.name,
    size: file.size,
    mime_type: file.mime_type,
    objectUrl: file.objectUrl || null,
    status: file.status || 'done',
    error: file.error || null,
  }))
  const [files, setFiles] = useState(() => normalizedInitialFiles)
  // Keep a ref in sync so the unmount cleanup can revoke object URLs
  // without closing over a stale `files` state value.
  const filesRef = useRef(files)
  filesRef.current = files
  const onFilesChangeRef = useRef(onFilesChange)
  onFilesChangeRef.current = onFilesChange

  function commitFiles(nextOrUpdater) {
    const next = typeof nextOrUpdater === 'function'
      ? nextOrUpdater(filesRef.current)
      : nextOrUpdater
    filesRef.current = next
    setFiles(next)
    onFilesChangeRef.current?.(next)
    return next
  }

  // Revoke any surviving object URLs when the component unmounts —
  // e.g. the user navigated away while files were still staged.
  useEffect(() => () => {
    for (const f of filesRef.current) {
      if (f.objectUrl) URL.revokeObjectURL(f.objectUrl)
    }
  }, [])

  async function addFiles(fileList) {
    if (!fileList.length) return

    const newChips = fileList.map(f => ({
      id: crypto.randomUUID(),
      name: f.name,
      size: f.size,
      mime_type: f.type,
      objectUrl: f.type.startsWith('image/') ? URL.createObjectURL(f) : null,
      status: 'uploading',
      error: null,
    }))
    commitFiles(prev => [...prev, ...newChips])

    for (let i = 0; i < newChips.length; i++) {
      const chip = newChips[i]
      try {
        // Can't use apiFetch here: multipart requires the browser to set
        // Content-Type with the boundary, which apiFetch overrides with JSON.
        const fd = new FormData()
        fd.append('files', fileList[i])
        const res = await fetch(`${BASE}/api/chats/${chatId}/uploads`, {
          method: 'POST',
          headers: getAuthHeaders(),
          body: fd,
        })
        if (!res.ok) {
          const msg = await res.text().catch(() => 'Upload failed')
          commitFiles(prev => prev.map(c =>
            c.id === chip.id ? { ...c, status: 'error', error: msg } : c
          ))
        } else {
          // Update name from server response (sanitized filename).
          const data = await res.json().catch(() => [])
          const serverName = data?.[0]?.name
          commitFiles(prev => prev.map(c =>
            c.id === chip.id
              ? { ...c, status: 'done', ...(serverName ? { name: serverName } : {}) }
              : c
          ))
        }
      } catch (err) {
        commitFiles(prev => prev.map(c =>
          c.id === chip.id ? { ...c, status: 'error', error: err.message } : c
        ))
      }
    }
  }

  function removeFile(id) {
    // Extract the side effects (URL revoke + network DELETE) from the
    // setFiles updater. React may double-invoke state updaters in
    // Strict Mode, which would fire two DELETE requests for the same
    // file. Compute the next state first, then apply side effects once.
    const removing = filesRef.current.find(c => c.id === id)
    if (removing?.objectUrl) URL.revokeObjectURL(removing.objectUrl)
    commitFiles(prev => prev.filter(c => c.id !== id))
    if (removing?.status === 'done' && removing.name) {
      fetch(`${BASE}/api/chats/${chatId}/uploads/${encodeURIComponent(removing.name)}`, {
        method: 'DELETE',
        headers: getAuthHeaders(),
      }).catch(() => {})
    }
  }

  function releaseFiles(fileList) {
    for (const f of fileList || []) {
      if (f.objectUrl) URL.revokeObjectURL(f.objectUrl)
    }
  }

  function clearFiles({ revoke = true } = {}) {
    const current = filesRef.current
    if (revoke) releaseFiles(current)
    commitFiles([])
  }

  function restoreFiles(fileList) {
    const restored = Array.isArray(fileList) ? fileList : []
    commitFiles(restored)
  }

  return { files, addFiles, removeFile, clearFiles, restoreFiles, releaseFiles }
}
