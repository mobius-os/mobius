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

// Enforce the list invariant in one place: id-deduped with the LAST
// occurrence winning (a rebuild re-surfaces at the end) and capped at the
// newest MAX. Both the writer and the persistence reader route through this,
// so a tampered or legacy stored value can never restore a list the writer
// could not have produced.
function normalizeBuiltAppList(list) {
  const seen = new Set()
  const newestFirst = []
  for (let i = list.length - 1; i >= 0; i--) {
    const app = list[i]
    const id = Number(app.id)
    if (seen.has(id)) continue
    seen.add(id)
    newestFirst.push(app)
  }
  return newestFirst.reverse().slice(-MAX_BUILT_APPS_PER_CHAT)
}

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
  return {
    ...(builtAppsByChatId || {}),
    [key]: normalizeBuiltAppList([...current, builtApp]),
  }
}

export function withoutBuiltAppForChat(builtAppsByChatId, chatId) {
  const key = chatKey(chatId)
  if (!key || !builtAppsByChatId?.[key]) return builtAppsByChatId || {}
  const { [key]: _removed, ...rest } = builtAppsByChatId
  return rest
}

// Drop CTA entries whose app no longer exists. sessionStorage can restore a
// CTA for an app deleted since it was persisted; without this, tapping it
// would navigate to a dead canvas instead of the guarded open-app path. Shell
// calls this against every live-fetched apps list, which also retires CTAs
// when an app is deleted mid-session. Returns the SAME reference when nothing
// changed so a setState with the result is a React no-op.
export function prunedBuiltAppsByChat(builtAppsByChatId, liveAppIds) {
  if (!builtAppsByChatId || typeof builtAppsByChatId !== 'object') return {}
  let changed = false
  const out = {}
  for (const [key, apps] of Object.entries(builtAppsByChatId)) {
    const kept = apps.filter(app => liveAppIds.has(Number(app.id)))
    if (kept.length === apps.length) {
      out[key] = apps
    } else {
      changed = true
      if (kept.length > 0) out[key] = kept
    }
  }
  return changed ? out : builtAppsByChatId
}

// Normalize a value parsed from sessionStorage (item 2b persistence) into the
// list shape. Tolerant on READ: a legacy one-app-per-chat scalar becomes a
// single-element list, entries missing an id are dropped, and a non-object
// yields an empty map — so a restore can never hand ChatView a value it can't
// map over. Restored arrays pass through the same dedupe + cap as the writer.
export function coerceBuiltAppsByChat(parsed) {
  if (!parsed || typeof parsed !== 'object') return {}
  const out = {}
  for (const [key, value] of Object.entries(parsed)) {
    if (Array.isArray(value)) {
      const apps = normalizeBuiltAppList(
        value.filter(app => app && app.id != null))
      if (apps.length > 0) out[key] = apps
    } else if (value && value.id != null) {
      out[key] = [value]
    }
  }
  return out
}
