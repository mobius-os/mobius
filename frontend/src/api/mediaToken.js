/**
 * Short-lived media tokens for serving uploads, media, and generated images.
 *
 * The serve routes (/api/chats/{id}/{uploads,media,generated}/{file} — `media` is
 * where agent screenshots + image-gen land; `generated` is the legacy alias)
 * accept the auth token from a ?token= query param because <img> tags can't set
 * Authorization headers. Passing the full 30-day owner JWT as a query param leaks it
 * into server access logs, browser history, and Referer headers.
 *
 * Instead, this helper fetches a 15-minute media-scoped token from
 * POST /api/chats/{id}/media-token, caches it for ~10 minutes per chat, and
 * auto-refreshes on 401. The serve routes only accept media-scoped tokens on
 * ?token= — owner JWTs are explicitly rejected.
 */

import { BASE, apiFetch } from './client.js'

// Per-chat token cache: { token: string, expiresAt: number }
// Cache for 10 minutes (server TTL is 15 min, giving 5 min buffer for clock skew
// and the window between fetch and use).
const _CACHE_MS = 10 * 60 * 1000
const _cache = new Map()

/**
 * Returns a ?token=<media-token> query suffix for media URLs on the given chat.
 *
 * Fetches a media token if none is cached or the cached one is about to expire.
 * Returns an empty string if token fetch fails (the request will 401, but the
 * image just won't render rather than crashing the page).
 *
 * @param {string} chatId
 * @returns {Promise<string>} e.g. "?token=eyJ..." or ""
 */
export async function mediaTokenParam(chatId) {
  const cached = _cache.get(chatId)
  if (cached && cached.expiresAt > Date.now()) {
    return `?token=${cached.token}`
  }
  try {
    const res = await apiFetch(`/chats/${chatId}/media-token`, { method: 'POST' })
    if (!res.ok) return ''
    const data = await res.json()
    if (!data.token) return ''
    _cache.set(chatId, {
      token: data.token,
      expiresAt: Date.now() + _CACHE_MS,
    })
    return `?token=${data.token}`
  } catch {
    return ''
  }
}

/**
 * Builds a full media URL for a chat resource (upload or generated image).
 * Appends the cached/fetched media token as a query param.
 *
 * @param {string} path  e.g. "/api/chats/{id}/uploads/{file}"
 * @param {string} chatId
 * @returns {Promise<string>}
 */
export async function buildMediaUrl(path, chatId) {
  const param = await mediaTokenParam(chatId)
  return `${BASE}${path}${param}`
}

/**
 * Clears all cached media tokens. Called on logout so tokens don't persist
 * to the next user on a shared device.
 */
export function clearMediaTokenCache() {
  _cache.clear()
}
