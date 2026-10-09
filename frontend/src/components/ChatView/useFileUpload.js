import { useState, useRef, useEffect, useCallback } from 'react'
import { getAuthHeaders, BASE } from '../../api/client.js'

/**
 * Hook encapsulating file upload state and API calls for chat attachments.
 *
 * @param {{ chatId: string, initialFiles?: Array }} options
 * @returns {{
 *   files: Array,
 *   addFiles: (fileList: File[]) => Promise<void>,
 *   removeFile: (id: string) => void,
 *   discardFiles: () => void,
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
  // In-flight uploads the caller already gave up on (removed, discarded or
  // unmounted): their late success is discarded instead of shown.
  const discardedIds = useRef(new Set())
  const onFilesChangeRef = useRef(onFilesChange)
  onFilesChangeRef.current = onFilesChange

  // Every action below is wrapped in useCallback and reads its inputs through
  // refs, so the returned identities only change when `chatId` does. Callers
  // put these in dependency arrays (ChatView's `doSend`), and an unstable
  // identity there re-allocates `doSend` on every render, which breaks
  // MsgContent's memo and re-renders the whole transcript on each keystroke.
  const commitFiles = useCallback((nextOrUpdater) => {
    const next = typeof nextOrUpdater === 'function'
      ? nextOrUpdater(filesRef.current)
      : nextOrUpdater
    filesRef.current = next
    setFiles(next)
    onFilesChangeRef.current?.(next)
    return next
  }, [])

  // Revoke any surviving object URLs when the component unmounts —
  // e.g. the user navigated away while files were still staged.
  useEffect(() => () => {
    for (const f of filesRef.current) {
      if (f.objectUrl) URL.revokeObjectURL(f.objectUrl)
      // Completed uploads are durable drafts. In-flight uploads have no saved
      // server name yet, and cannot be restored after this hook unmounts.
      if (f.status === 'uploading') discardedIds.current.add(f.id)
    }
  }, [])

  const discardUpload = useCallback((file) => {
    // The server deletes only drafts no sent message or answer has claimed,
    // so a stale chip or late cleanup can never delete a file in use.
    if (!file?.name) return
    fetch(`${BASE}/api/chats/${chatId}/uploads/${encodeURIComponent(file.name)}`, {
      method: 'DELETE', headers: getAuthHeaders(),
    }).catch(() => {})
  }, [chatId])

  const addFiles = useCallback(async (fileList) => {
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
          const uploaded = data?.[0]
          if (!uploaded?.name) throw new Error('Upload response is missing file metadata')
          if (discardedIds.current.has(chip.id)) {
            discardUpload(uploaded)
            continue
          }
          commitFiles(prev => prev.map(c =>
            c.id === chip.id
              ? { ...c, name: uploaded.name, size: uploaded.size, mime_type: uploaded.mime_type, status: 'done' }
              : c
          ))
        }
      } catch (err) {
        commitFiles(prev => prev.map(c =>
          c.id === chip.id ? { ...c, status: 'error', error: err.message } : c
        ))
      } finally {
        discardedIds.current.delete(chip.id)
      }
    }
  }, [chatId, commitFiles, discardUpload])

  const removeFile = useCallback((id) => {
    // Extract the side effects (URL revoke + network DELETE) from the
    // setFiles updater. React may double-invoke state updaters in
    // Strict Mode, which would fire two DELETE requests for the same
    // file. Compute the next state first, then apply side effects once.
    const removing = filesRef.current.find(c => c.id === id)
    if (removing?.objectUrl) URL.revokeObjectURL(removing.objectUrl)
    commitFiles(prev => prev.filter(c => c.id !== id))
    if (removing?.status === 'uploading') discardedIds.current.add(id)
    if (removing?.status === 'done') discardUpload(removing)
  }, [commitFiles, discardUpload])

  const releaseFiles = useCallback((fileList) => {
    for (const f of fileList || []) {
      if (f.objectUrl) URL.revokeObjectURL(f.objectUrl)
    }
  }, [])

  const clearFiles = useCallback(({ revoke = true } = {}) => {
    const current = filesRef.current
    if (revoke) releaseFiles(current)
    commitFiles([])
  }, [releaseFiles, commitFiles])

  // Drop every held file. The server keeps any a sent message or answer has
  // already claimed, so callers need not work out which ones were sent.
  const discardFiles = useCallback(() => {
    for (const file of filesRef.current) {
      // A failed chip never got a server name; its local name could belong
      // to someone else's draft, so only finished uploads are discarded.
      if (file.status === 'uploading') discardedIds.current.add(file.id)
      else if (file.status === 'done') discardUpload(file)
    }
    clearFiles()
  }, [clearFiles, discardUpload])

  const restoreFiles = useCallback((fileList) => {
    const restored = Array.isArray(fileList) ? fileList : []
    commitFiles(restored)
  }, [commitFiles])

  return { files, addFiles, removeFile, clearFiles, restoreFiles, releaseFiles, discardFiles }
}
