// Chat markdown media uses one authenticated original URL for disclosure and
// an optional display-sized derivative for the inline transcript. Keep that
// distinction at the URL boundary so galleries, lightboxes, and downloads all
// continue to receive the original without rebuilding query strings.

const CHAT_MEDIA_PATH_RE = /^(?:.*)?\/api\/chats\/([^/]+)\/(uploads|media)\//

export function getMediaChatId(src) {
  const match = String(src || '').match(CHAT_MEDIA_PATH_RE)
  return match ? match[1] : null
}

export function previewSrcForChatMedia(src) {
  const value = String(src || '')
  const match = value.match(CHAT_MEDIA_PATH_RE)
  if (!match) return src

  try {
    const absolute = /^[a-z][a-z0-9+.-]*:/i.test(value)
    const url = new URL(value, 'https://mobius.local')
    url.searchParams.set('preview', 'true')
    return absolute
      ? url.href
      : `${url.pathname}${url.search}${url.hash}`
  } catch {
    return src
  }
}
