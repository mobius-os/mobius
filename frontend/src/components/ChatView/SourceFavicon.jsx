import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'

const MAX_FAVICON_BYTES = 256 * 1024
const FAVICON_TIMEOUT_MS = 8000
const ACCEPTED_CONTENT_TYPES = new Set([
  'application/octet-stream',
  'image/gif',
  'image/ico',
  'image/jpeg',
  'image/png',
  'image/svg+xml',
  'image/vnd.microsoft.icon',
  'image/webp',
  'image/x-icon',
])

// Components for repeated citations share the same read. Once it settles, the
// service worker's existing proxy cache owns reuse across renders and sessions.
const pendingFavicons = new Map()

function hasBytes(bytes, signature, offset = 0) {
  return bytes.length >= offset + signature.length
    && signature.every((byte, index) => bytes[offset + index] === byte)
}

function detectedImageType(bytes) {
  if (hasBytes(bytes, [0x00, 0x00, 0x01, 0x00])) return 'image/x-icon'
  if (hasBytes(bytes, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) {
    return 'image/png'
  }
  if (
    hasBytes(bytes, [0x47, 0x49, 0x46, 0x38])
    && [0x37, 0x39].includes(bytes[4])
    && bytes[5] === 0x61
  ) return 'image/gif'
  if (hasBytes(bytes, [0xff, 0xd8, 0xff])) return 'image/jpeg'
  if (
    hasBytes(bytes, [0x52, 0x49, 0x46, 0x46])
    && hasBytes(bytes, [0x57, 0x45, 0x42, 0x50], 8)
  ) return 'image/webp'
  return ''
}

export function safeSvgFavicon(source) {
  if (typeof source !== 'string') return false
  const value = source.trim()
  if (!/^(?:<\?xml\b[^?]*\?>\s*)?<svg\b/i.test(value)) return false
  // SVG loaded through <img> uses the browser's static image mode, but keep
  // the blob self-contained as a second boundary: no active elements, inline
  // handlers, stylesheets, or external subresources.
  return !(
    /<!doctype\b/i.test(value)
    || /<(?:script|style|foreignObject|iframe|object|embed|image|a)\b/i.test(value)
    || /\son[a-z][a-z0-9_-]*\s*=/i.test(value)
    || /\b(?:href|xlink:href)\s*=\s*["'](?!#)/i.test(value)
    || /@import\b|url\s*\(/i.test(value)
  )
}

export function sourceFaviconProxyPath(faviconUrl) {
  try {
    const parsed = new URL(faviconUrl)
    if (!['http:', 'https:'].includes(parsed.protocol)) return ''
    if (!parsed.host || parsed.username || parsed.password) return ''
    return `/proxy?url=${encodeURIComponent(parsed.href)}`
  } catch {
    return ''
  }
}

export function sourceFaviconResolverPath(discoveryUrl) {
  try {
    const parsed = new URL(discoveryUrl)
    if (!['http:', 'https:'].includes(parsed.protocol)) return ''
    if (!parsed.host || parsed.username || parsed.password) return ''
    return `/proxy/favicon?url=${encodeURIComponent(parsed.href)}`
  } catch {
    return ''
  }
}

export function sourceFaviconCandidateUrls(faviconUrl) {
  try {
    const parsed = new URL(faviconUrl)
    if (!['http:', 'https:'].includes(parsed.protocol)) return []
    if (!parsed.host || parsed.username || parsed.password) return []
    return [...new Set([
      parsed.href,
      new URL('/favicon.svg', parsed).href,
      new URL('/favicon.png', parsed).href,
      new URL('/apple-touch-icon.png', parsed).href,
    ])]
  } catch {
    return []
  }
}

export async function validatedFaviconBlob(response) {
  if (!response.ok) throw new Error(`Favicon read failed: ${response.status}`)

  const declaredType = (response.headers.get('content-type') || '')
    .split(';', 1)[0]
    .trim()
    .toLowerCase()
  if (declaredType && !ACCEPTED_CONTENT_TYPES.has(declaredType)) {
    throw new Error(`Unsupported favicon content type: ${declaredType}`)
  }

  const declaredLength = Number(response.headers.get('content-length'))
  if (Number.isFinite(declaredLength) && declaredLength > MAX_FAVICON_BYTES) {
    throw new Error('Favicon is too large')
  }

  const blob = await response.blob()
  if (!blob.size || blob.size > MAX_FAVICON_BYTES) {
    throw new Error('Favicon is empty or too large')
  }
  if (declaredType === 'image/svg+xml') {
    const source = await blob.text()
    if (!safeSvgFavicon(source)) {
      throw new Error('SVG favicon is not a safe self-contained image')
    }
    return new Blob([source], { type: 'image/svg+xml' })
  }
  const prefix = new Uint8Array(await blob.slice(0, 12).arrayBuffer())
  const detectedType = detectedImageType(prefix)
  if (!detectedType) throw new Error('Favicon bytes are not a supported image')

  // Give the browser the sniffed type without trusting the remote server's
  // declaration; application/octet-stream is especially common for .ico.
  return blob.type === detectedType
    ? blob
    : new Blob([blob], { type: detectedType })
}

export function loadSourceFavicon(faviconUrl, discoveryUrl = '') {
  const candidates = sourceFaviconCandidateUrls(faviconUrl)
  if (candidates.length === 0) {
    return Promise.reject(new Error('Invalid favicon URL'))
  }

  const resolverPath = sourceFaviconResolverPath(discoveryUrl)
  const key = [...candidates, resolverPath].join('\n')
  const existing = pendingFavicons.get(key)
  if (existing) return existing

  const request = (async () => {
    let lastError = new Error('Favicon unavailable')
    if (resolverPath) {
      try {
        const response = await apiFetch(resolverPath, {
          timeoutMs: FAVICON_TIMEOUT_MS,
        })
        return await validatedFaviconBlob(response)
      } catch (error) {
        // Keep the direct candidates as a compatibility path while a newly
        // added resolver is waiting for a server restart.
        lastError = error
      }
    }
    for (const candidate of candidates) {
      try {
        const response = await apiFetch(sourceFaviconProxyPath(candidate), {
          timeoutMs: FAVICON_TIMEOUT_MS,
        })
        return await validatedFaviconBlob(response)
      } catch (error) {
        lastError = error
      }
    }
    throw lastError
  })().finally(() => pendingFavicons.delete(key))
  pendingFavicons.set(key, request)
  return request
}

function useNearViewport() {
  const ref = useRef(null)
  const [near, setNear] = useState(false)

  useEffect(() => {
    const node = ref.current
    if (!node) return undefined
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true)
      return undefined
    }
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) setNear(true)
    }, { rootMargin: '240px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  return [ref, near]
}

export default function SourceFavicon({ faviconUrl, discoveryUrl, fallback }) {
  const [hostRef, near] = useNearViewport()
  const [objectUrl, setObjectUrl] = useState('')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    if (!near || !faviconUrl) return undefined
    let disposed = false
    let ownedUrl = ''
    setLoaded(false)

    loadSourceFavicon(faviconUrl, discoveryUrl).then((blob) => {
      const nextUrl = URL.createObjectURL(blob)
      if (disposed) {
        URL.revokeObjectURL(nextUrl)
        return
      }
      ownedUrl = nextUrl
      setObjectUrl(nextUrl)
    }).catch(() => {
      if (!disposed) setObjectUrl('')
    })

    return () => {
      disposed = true
      if (ownedUrl) URL.revokeObjectURL(ownedUrl)
    }
  }, [discoveryUrl, faviconUrl, near])

  const discardObjectUrl = () => {
    if (objectUrl) URL.revokeObjectURL(objectUrl)
    setObjectUrl('')
    setLoaded(false)
  }

  return (
    <span ref={hostRef} className="chat__source-icon" aria-hidden="true">
      <span className="chat__source-fallback">{fallback}</span>
      {objectUrl && (
        <img
          className={`chat__source-favicon${loaded ? ' chat__source-favicon--loaded' : ''}`}
          src={objectUrl}
          alt=""
          width="16"
          height="16"
          decoding="async"
          onLoad={() => setLoaded(true)}
          onError={discardObjectUrl}
        />
      )}
    </span>
  )
}
