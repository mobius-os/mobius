// AppOut owns icon existence, identity, and cache version. Callers only select
// the bounded rendition they need; they never reconstruct or probe the asset.
const readyIconUrls = new Set()

const APP_ICON_READY_CACHE_MAX = 256

export function appInitials(name) {
  const words = String(name || '')
    .replace(/[^a-z0-9]+/gi, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
  if (words.length === 0) return 'A'
  if (words.length === 1) return words[0].slice(0, 2).toLocaleUpperCase()
  return `${words[0][0]}${words[1][0]}`.toLocaleUpperCase()
}

export function appIconUrl(app, size = 128) {
  if (!app?.icon_url) return null
  if (size == null) return app.icon_url
  const separator = app.icon_url.includes('?') ? '&' : '?'
  return `${app.icon_url}${separator}size=${size}`
}

export function appIconIsReady(url) {
  return !!url && readyIconUrls.has(url)
}

export function rememberAppIconReady(url) {
  if (!url) return
  // Refresh insertion order so current versions outlive stale versions during
  // a long app-building session without letting old icon URLs grow forever.
  readyIconUrls.delete(url)
  readyIconUrls.add(url)
  if (readyIconUrls.size > APP_ICON_READY_CACHE_MAX) {
    readyIconUrls.delete(readyIconUrls.values().next().value)
  }
}

export function forgetAppIconReady(url) {
  if (url) readyIconUrls.delete(url)
}

/**
 * Fetch and decode one versioned icon ahead of its first visible mount.
 * Failures stay an ordinary initials fallback and can be retried by a later
 * visible <img>.
 */
function preloadAppIcon(url, ImageCtor) {
  if (!url || typeof ImageCtor !== 'function') return Promise.resolve(false)
  if (readyIconUrls.has(url)) return Promise.resolve(true)

  return new Promise(resolve => {
    const image = new ImageCtor()
    image.onload = async () => {
      try { await image.decode?.() } catch { /* onload already proved usable bytes */ }
      rememberAppIconReady(url)
      resolve(true)
    }
    image.onerror = () => { resolve(false) }
    image.decoding = 'async'
    image.src = url
  })
}

export function preloadAppIcons(apps, {
  size = 128,
  ImageCtor = globalThis.Image,
} = {}) {
  const urls = new Set(
    (apps || []).map(app => appIconUrl(app, size)).filter(Boolean),
  )
  return Promise.all([...urls].map(url => preloadAppIcon(url, ImageCtor)))
}
