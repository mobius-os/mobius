// Shared whitelist parser for notification `target` deep-links.
//
// TRUST BOUNDARY: notification rows are writable by app-scoped tokens
// (POST /api/notifications/send), and the backend does not validate `target`,
// so every consumer must treat it as hostile input. This parser is consumed by
// BOTH the NotificationsView row click and Shell's warm-tap notification-click
// handler, so the two can never drift; sw.js keeps its own `_safeTarget` copy
// (separate bundle) with the same posture. Never navigate a raw target.
//
// Posture mirrors sw.js::_safeTarget: same-origin only for absolute URLs,
// known in-scope forms only, conservative id charsets, and FAIL CLOSED — any
// unrecognized or malformed target parses to null (a no-op for the caller),
// never to a best-effort navigation.
//
// Accepted forms (the shapes locked by backend/tests/test_notification_target.py
// plus the notifications page):
//   /shell/?app=<id-or-slug>[&intent=...]  → { view: 'canvas', app, intent }
//   /shell/?chat=<id>                      → { view: 'chat', chatId }
//   /app/<numeric-id>   (legacy)           → { view: 'canvas', app, intent: null }
//   /chat/<id>          (legacy)           → { view: 'chat', chatId }
//
// `app` is returned as the RAW accepted string (id or slug) because the shell
// resolves slugs via openAppWithIntent, exactly like the cold deepLink parser.

const ID_RE = /^[A-Za-z0-9_-]+$/
// Ids plus the ':' and '.' that namespace an intent's target (see sw.js).
const INTENT_RE = /^[A-Za-z0-9_.:-]{1,128}$/

export function parseNotificationTarget(target) {
  if (typeof target !== 'string' || !target) return null
  let path = target
  let search = ''
  try {
    if (/^https?:\/\//i.test(target)) {
      const origin = globalThis.location?.origin
      const u = new URL(target)
      // Unknown own-origin (non-browser context) fails closed too.
      if (!origin || u.origin !== origin) return null
      path = u.pathname
      search = u.search
    } else {
      const q = target.indexOf('?')
      if (q !== -1) {
        path = target.slice(0, q)
        search = target.slice(q)
      }
    }
  } catch {
    return null
  }

  if (/^\/shell\/?$/.test(path)) {
    let params
    try {
      params = new URLSearchParams(search)
    } catch {
      return null
    }
    const app = params.get('app')
    const chat = params.get('chat')
    if (app && ID_RE.test(app)) {
      const intent = params.get('intent')
      return {
        view: 'canvas',
        app,
        intent: (intent && INTENT_RE.test(intent)) ? intent : null,
      }
    }
    if (chat && ID_RE.test(chat)) return { view: 'chat', chatId: chat }
    return null
  }

  // Legacy out-of-scope forms, still present on old notification rows.
  const appMatch = path.match(/^\/app\/(\d+)$/)
  if (appMatch) return { view: 'canvas', app: appMatch[1], intent: null }
  const chatMatch = path.match(/^\/chat\/([A-Za-z0-9_-]+)$/)
  if (chatMatch) return { view: 'chat', chatId: chatMatch[1] }
  return null
}
