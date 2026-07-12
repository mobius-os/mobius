// The built-app CTA row is DERIVED from server truth — the apps query's own
// `chat_id` column — never mirrored in client state. Every app row carries the
// chat_id of the turn that built it (register_app.py stamps it), so the CTA
// list for a chat is exactly "the apps this chat owns," durably, across
// sessions and devices. A chat can build several apps in one turn ("a notes app
// and a habit tracker"), so it is a LIST, newest last, capped so the chat foot
// can't grow without bound.
const MAX_BUILT_APPS_PER_CHAT = 3

// Shared frozen empty list so a chat with no built apps always yields the SAME
// reference. ChatView depends on that stable identity: its preview and
// composer-height effects key on the list, and a fresh `[]` each call would
// fire them on every render.
const EMPTY_BUILT_APPS = Object.freeze([])

export function chatKey(chatId) {
  if (chatId === null || chatId === undefined || chatId === '') return null
  return String(chatId)
}

// The apps this chat owns, oldest-first, capped to the newest few. Shared by
// the signature and the projection so the two can never disagree on membership
// or order.
function scopedApps(apps, chatId) {
  const key = chatKey(chatId)
  if (!key || !Array.isArray(apps)) return []
  return apps
    .filter(a => chatKey(a.chat_id) === key)
    .sort((x, y) =>
      String(x.updated_at || '').localeCompare(String(y.updated_at || '')))
    .slice(-MAX_BUILT_APPS_PER_CHAT)
}

// A primitive signature (`id:updated_at` per app, joined) over the derived
// list. Shell memoizes the derived array on THIS string, so an unrelated
// app_updated refetch — a new `apps` array with identical relevant content —
// yields the SAME array reference and does NOT re-fire ChatView's
// builtApps-keyed effects. Referential stability is the load-bearing invariant:
// without it every app_updated (any app, any chat) would churn the CTA effects.
export function builtAppsSignature(apps, chatId) {
  return scopedApps(apps, chatId)
    .map(a => `${a.id}:${a.updated_at ?? ''}`)
    .join(',')
}

// The built-app CTA list for a chat, projected to the minimal shape the CTA row
// and the pulse decision need. Returns the shared frozen empty list when the
// chat owns no apps.
export function derivedBuiltApps(apps, chatId) {
  const scoped = scopedApps(apps, chatId)
  if (scoped.length === 0) return EMPTY_BUILT_APPS
  return scoped.map(a => ({ id: a.id, name: a.name, updated_at: a.updated_at }))
}
