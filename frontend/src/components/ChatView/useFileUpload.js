import { useState } from 'react'
import { getToken } from '../../api/client.js'

/**
 * Hook encapsulating file upload state and API calls for chat attachments.
 *
 * @param {{ chatId: string }} options
 * @returns {{ files: Array, addFiles: (fileList: File[]) => Promise<void>, removeFile: (id: string) => void, clearFiles: () => void }}
 */
export default function useFileUpload({ chatId }) {
  const [files, setFiles] = useState([])

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
    setFiles(prev => [...prev, ...newChips])

    for (let i = 0; i < newChips.length; i++) {
      const chip = newChips[i]
      try {
        // Can't use apiFetch here: multipart requires the browser to set
        // Content-Type with the boundary, which apiFetch overrides with JSON.
        const fd = new FormData()
        fd.append('files', fileList[i])
        const res = await fetch(`/api/chats/${chatId}/uploads`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${getToken()}` },
          body: fd,
        })
        if (!res.ok) {
          const msg = await res.text().catch(() => 'Upload failed')
          setFiles(prev => prev.map(c =>
            c.id === chip.id ? { ...c, status: 'error', error: msg } : c
          ))
        } else {
          // Update name from server response (sanitized filename).
          const data = await res.json().catch(() => [])
          const serverName = data?.[0]?.name
          setFiles(prev => prev.map(c =>
            c.id === chip.id
              ? { ...c, status: 'done', ...(serverName ? { name: serverName } : {}) }
              : c
          ))
        }
      } catch (err) {
        setFiles(prev => prev.map(c =>
          c.id === chip.id ? { ...c, status: 'error', error: err.message } : c
        ))
      }
    }
  }

  function removeFile(id) {
    setFiles(prev => {
      const removing = prev.find(c => c.id === id)
      if (removing?.objectUrl) URL.revokeObjectURL(removing.objectUrl)
      return prev.filter(c => c.id !== id)
    })
  }

  function clearFiles() {
    files.forEach(f => { if (f.objectUrl) URL.revokeObjectURL(f.objectUrl) })
    setFiles([])
  }

  return { files, addFiles, removeFile, clearFiles }
}
