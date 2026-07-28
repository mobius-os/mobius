// Validate a post-authentication destination at one boundary. The returned
// value is always a same-origin absolute path with its query/hash preserved.
export function safeReturnPath(raw, origin) {
  if (typeof raw !== 'string' || !raw || typeof origin !== 'string' || !origin) {
    return null
  }
  if (!raw.startsWith('/')) return null
  if (raw.includes('\\') || /%5c/i.test(raw)) return null

  try {
    const url = new URL(raw, origin)
    if (url.origin !== origin) return null
    if (!url.pathname.startsWith('/') || url.pathname.startsWith('//')) return null
    if (decodeURIComponent(url.pathname).includes('\\')) return null
    return url.pathname + url.search + url.hash
  } catch {
    return null
  }
}
