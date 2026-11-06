/* Show an owner-authenticated image route (an avatar) as a blob URL, since an <img> cannot send the bearer header. */
import { useEffect, useState } from 'react'
import { apiFetch } from '../api/client.js'

/**
 * Fetches `path` (relative to /api, e.g. '/github/avatar') whenever `key`
 * changes, and returns an object URL, or null while loading, when `key` is
 * empty, or when the image is unavailable. Callers keep their own fallback
 * artwork for null. The URL is revoked when the key changes or on unmount.
 */
export default function useAuthedImageUrl(path, key) {
  const [url, setUrl] = useState(null)
  useEffect(() => {
    if (!path || !key) {
      setUrl(null)
      return undefined
    }
    const controller = new AbortController()
    let objectUrl = null
    apiFetch(path, { signal: controller.signal, timeoutMs: 15_000 }).then(async (response) => {
      if (!response.ok) return
      const blob = await response.blob()
      if (controller.signal.aborted || !blob.type.startsWith('image/')) return
      objectUrl = URL.createObjectURL(blob)
      setUrl(objectUrl)
    }).catch(() => { /* The caller's fallback artwork stays. */ })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
      setUrl(null)
    }
  }, [path, key])
  return url
}
