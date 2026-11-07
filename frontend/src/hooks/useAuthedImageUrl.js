/* Owner-authenticated images (avatars): fetched once per page load, kept in memory, and shown through one stable object URL so reopening Settings never blinks. */
import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../api/client.js'

// One object URL per blob for the life of the page. Remounts, and every
// component showing the same image, reuse it synchronously instead of minting
// a new URL a frame later. Avatars are a few kilobytes and change rarely, so a
// URL left behind when one changes costs nothing worth revoking it for.
const objectUrls = new WeakMap()

function objectUrlFor(blob) {
  if (!blob) return null
  let url = objectUrls.get(blob)
  if (!url) {
    url = URL.createObjectURL(blob)
    objectUrls.set(blob, url)
  }
  return url
}

// path + key -> { blob } once settled (blob null when unavailable), or
// { pending } while a fetch is in flight, so simultaneous mounts share one.
// A failed read settles as { blob: null, failed: true }: the caller shows its
// own artwork now, and the next mount tries again.
const images = new Map()
// Paths whose next read must skip the browser's HTTP cache, because the image
// behind them just changed (see forgetAuthedImage).
const changedPaths = new Set()

function cacheKeyFor(path, key) {
  return path && key ? `${path}\n${key}` : ''
}

function load(cacheKey, path) {
  const entry = images.get(cacheKey)
  if (entry && !entry.failed) return entry.pending || Promise.resolve()
  const cache = changedPaths.delete(path) ? 'no-cache' : 'default'
  const pending = apiFetch(path, { timeoutMs: 15_000, cache })
    .then(async (response) => {
      const blob = response.ok ? await response.blob() : null
      images.set(cacheKey, { blob: blob?.type?.startsWith('image/') ? blob : null })
    })
    .catch(() => { images.set(cacheKey, { blob: null, failed: true }) })
  images.set(cacheKey, { pending })
  return pending
}

/**
 * Drop every cached image read from `path`, e.g. after uploading a new photo
 * that may keep the same address. Mounted readers fetch it again, past the
 * browser's HTTP cache, on their next render.
 */
export function forgetAuthedImage(path) {
  for (const cacheKey of images.keys()) {
    if (cacheKey.startsWith(`${path}\n`)) images.delete(cacheKey)
  }
  changedPaths.add(path)
}

/**
 * `{ url, status }` for an owner-authenticated image route (relative to /api,
 * e.g. '/identity/avatar'). `key` names what the image is (an account or its
 * avatar address);
 * an empty key or path means no image. `status` is 'loading' until the first
 * read settles, then 'ready' with a url or 'none' when there is no image, so
 * callers can show a quiet placeholder rather than swapping artwork in.
 */
export default function useAuthedImageUrl(path, key) {
  const cacheKey = cacheKeyFor(path, key)
  const [, setSettled] = useState(0)
  // The image this mount already tried, so a failed read retries on the next
  // mount rather than in a loop.
  const tried = useRef('')
  const entry = cacheKey ? images.get(cacheKey) : null
  // A forgotten image is read again on the next render, not only on remount.
  const missing = Boolean(cacheKey) && !entry

  useEffect(() => {
    if (!cacheKey) return undefined
    const current = images.get(cacheKey)
    if (current && !current.pending && (!current.failed || tried.current === cacheKey)) return undefined
    tried.current = cacheKey
    let active = true
    load(cacheKey, path).then(() => { if (active) setSettled(n => n + 1) })
    return () => { active = false }
  }, [cacheKey, path, missing])

  if (!cacheKey) return { url: null, status: 'none' }
  if (!entry || entry.pending) return { url: null, status: 'loading' }
  return entry.blob
    ? { url: objectUrlFor(entry.blob), status: 'ready' }
    : { url: null, status: 'none' }
}
