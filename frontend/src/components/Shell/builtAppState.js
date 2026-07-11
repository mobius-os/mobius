// A chat can birth several apps in a single turn ("a notes app and a habit
// tracker"), so each chat holds an ORDERED LIST of built apps (most recent
// last) rather than a single one — ChatView renders one CTA row per entry.
// The list dedups by id (a rebuild moves the app to the end instead of
// duplicating its row) and keeps only the newest few so the chat foot can't
// grow without bound.
const MAX_BUILT_APPS_PER_CHAT = 3

// Shared frozen empty list so a chat with no built apps always yields the
// SAME reference. ChatView depends on that stable identity: its preview and
// composer-height effects key on the list, and a fresh `[]` each call would
// fire them on every render.
const EMPTY_BUILT_APPS = Object.freeze([])

export function chatKey(chatId) {
  if (chatId === null || chatId === undefined || chatId === '') return null
  return String(chatId)
}

export function builtAppsForChat(builtAppsByChatId, chatId) {
  const key = chatKey(chatId)
  if (!key) return EMPTY_BUILT_APPS
  return builtAppsByChatId?.[key] || EMPTY_BUILT_APPS
}

export function withBuiltAppForChat(builtAppsByChatId, chatId, builtApp) {
  const key = chatKey(chatId)
  if (!key || !builtApp?.id) return builtAppsByChatId || {}
  const current = builtAppsByChatId?.[key] || []
  // Drop any earlier entry for this id so a rebuild re-surfaces the app at
  // the end (most recent) instead of leaving a stale duplicate row behind.
  const deduped = current.filter(app => Number(app.id) !== Number(builtApp.id))
  const next = [...deduped, builtApp].slice(-MAX_BUILT_APPS_PER_CHAT)
  return {
    ...(builtAppsByChatId || {}),
    [key]: next,
  }
}

export function withoutBuiltAppForChat(builtAppsByChatId, chatId) {
  const key = chatKey(chatId)
  if (!key || !builtAppsByChatId?.[key]) return builtAppsByChatId || {}
  const { [key]: _removed, ...rest } = builtAppsByChatId
  return rest
}

// Normalize a value parsed from sessionStorage (item 2b persistence) into the
// list shape. Tolerant on READ: a legacy one-app-per-chat scalar becomes a
// single-element list, entries missing an id are dropped, and a non-object
// yields an empty map — so a restore can never hand ChatView a value it can't
// map over.
export function coerceBuiltAppsByChat(parsed) {
  if (!parsed || typeof parsed !== 'object') return {}
  const out = {}
  for (const [key, value] of Object.entries(parsed)) {
    if (Array.isArray(value)) {
      const apps = value.filter(app => app && app.id != null)
      if (apps.length > 0) out[key] = apps
    } else if (value && value.id != null) {
      out[key] = [value]
    }
  }
  return out
}
