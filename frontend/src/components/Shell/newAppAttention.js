// Pure helpers for the drawer's "new app arrived" dot. A freshly built or
// installed app lands at the bottom of the oldest-first unpinned list with no
// affordance; Shell flags ids that appear AFTER a per-session baseline and the
// drawer renders a subtle accent dot until the app is first opened. Mirrors the
// attentionChatIds mechanism.
//
// Shell owns the stateful pieces (the baseline ref, the flagged-id Set); these
// functions hold the set arithmetic so the semantics are testable without
// rendering the shell. Everything coerces to Number so a string id from a
// restored route can't shadow the numeric id from the apps query.

// Ids in the current apps list that the session has not accounted for yet —
// genuine arrivals since the baseline was captured.
export function freshAppIds(accountedFor, currentIds) {
  const seen = accountedFor instanceof Set ? accountedFor : new Set(accountedFor)
  const out = []
  for (const raw of currentIds || []) {
    const id = Number(raw)
    if (!Number.isNaN(id) && !seen.has(id)) out.push(id)
  }
  return out
}

// Project fresh app ids onto the durable relationship the workspace cares
// about: which chat produced which runnable artifact. Store installs have no
// chat_id and remain drawer-only. Keeping this projection pure gives the future
// pane dispatcher the same input as today's flat tab strip.
export function freshChatBuiltApps(apps, freshIds) {
  if (!Array.isArray(apps) || !freshIds?.length) return []
  const fresh = new Set(freshIds.map(Number).filter(id => !Number.isNaN(id)))
  return apps.flatMap(app => {
    const appId = Number(app?.id)
    if (Number.isNaN(appId) || !fresh.has(appId) || app?.chat_id == null) return []
    return [{ appId, chatId: String(app.chat_id) }]
  })
}

// Flag ids immutably, returning the SAME set when nothing changed so React can
// bail out of a re-render (mirrors Shell's attentionChatIds setters).
export function withAppsFlagged(prev, ids) {
  if (!ids || ids.length === 0) return prev
  let changed = false
  const next = new Set(prev)
  for (const raw of ids) {
    const id = Number(raw)
    if (!Number.isNaN(id) && !next.has(id)) {
      next.add(id)
      changed = true
    }
  }
  return changed ? next : prev
}

// Clear one flag when its app is opened. Same-reference return on a no-op.
export function withoutAppFlagged(prev, id) {
  const n = Number(id)
  if (Number.isNaN(n) || !prev.has(n)) return prev
  const next = new Set(prev)
  next.delete(n)
  return next
}

// The shell has two honest reasons to mark an app: it arrived during this
// browser session, or a durable app-attributed background notification landed.
// Present them as one visual state to Drawer + WorkspaceChrome.
export function appAttentionIds(apps, newAppIds, visibleAppIds = []) {
  const next = new Set()
  const visible = new Set(
    [...visibleAppIds].map(Number).filter(id => !Number.isNaN(id)),
  )
  for (const raw of newAppIds || []) {
    const id = Number(raw)
    if (!Number.isNaN(id) && !visible.has(id)) next.add(id)
  }
  for (const app of apps || []) {
    const id = Number(app?.id)
    if (!Number.isNaN(id) && !visible.has(id) && app?.has_unseen_activity) next.add(id)
  }
  return next
}

// Optimistically clear the query-cache flag as soon as a visible app is
// acknowledged. A failed POST invalidates the list and restores server truth.
export function withAppActivitySeen(apps, appId, seenThroughVersion = Infinity, appCreatedAt) {
  const id = Number(appId)
  const seenThrough = Number(seenThroughVersion)
  if (!Array.isArray(apps) || Number.isNaN(id)) return apps
  let changed = false
  const next = apps.map(app => {
    if (Number(app?.id) !== id || !app?.has_unseen_activity ||
        (appCreatedAt !== undefined && app.created_at !== appCreatedAt)) return app
    const rowVersion = Number(app.unseen_activity_version)
    if (
      Number.isFinite(seenThrough) &&
      Number.isFinite(rowVersion) &&
      rowVersion > seenThrough
    ) return app
    changed = true
    return {
      ...app,
      has_unseen_activity: false,
      unseen_activity_version: null,
    }
  })
  return changed ? next : apps
}

// Apply one live app_activity marker to the cached list, so the drawer dot
// appears without re-downloading every app row. Returns null when the event
// carries no version or the app is not cached: the caller then refetches
// server truth. A version the shell already acknowledged never re-lights.
export function withAppActivity(apps, appId, activityVersion, { seenThrough, appCreatedAt } = {}) {
  const id = Number(appId)
  const version = Number(activityVersion)
  if (!Array.isArray(apps) || !Number.isSafeInteger(id) || id <= 0 || !Number.isSafeInteger(version) || version <= 0 || !appCreatedAt) {
    return null
  }
  let found = false
  let changed = false
  const next = apps.map(app => {
    if (Number(app?.id) !== id) return app
    if (app.created_at !== appCreatedAt) return app
    found = true
    if (Number.isFinite(Number(seenThrough)) && version <= Number(seenThrough)) {
      return app
    }
    const current = Number(app.unseen_activity_version)
    if (app.has_unseen_activity && Number.isFinite(current) && current >= version) {
      return app
    }
    changed = true
    return { ...app, has_unseen_activity: true, unseen_activity_version: version }
  })
  if (!found) return null
  return changed ? next : apps
}

// Seen receipts from this tab or another tab share one lifetime-bound floor.
// The server reports the version it actually cleared, not a caller's larger
// requested bound, so this can never hide future activity.
const appActivityLifetimeKey = (id, createdAt) => `${id}:${createdAt}`

export function rememberSeenAppActivity(seen, appId, appCreatedAt, version) {
  const id = Number(appId)
  const n = Number(version)
  if (!Number.isSafeInteger(id) || id <= 0 || !Number.isSafeInteger(n) || n <= 0 || !appCreatedAt) return
  const key = appActivityLifetimeKey(id, appCreatedAt)
  seen.set(key, Math.max(seen.get(key) ?? 0, n))
}

export function seenAppActivityVersion(seen, appId, appCreatedAt) {
  return seen.get(appActivityLifetimeKey(Number(appId), appCreatedAt))
}

// Own one exact app/version acknowledgement. The key is released before a
// failed request restores server truth, so the resulting refetch can retry
// immediately while the app is still visible. A successful request clears the
// cache again because an unrelated in-flight refetch may have restored the
// just-acknowledged version after the initial optimistic update.
export async function acknowledgeAppActivity({
  appId,
  activityVersion,
  appCreatedAt,
  inFlight,
  request,
  clearCached,
  confirmSeen = () => {},
  restoreServerTruth,
}) {
  const key = `${appId}:${activityVersion}${appCreatedAt ? `:${appCreatedAt}` : ''}`
  if (inFlight.has(key)) return false
  inFlight.add(key)
  clearCached(appId, activityVersion)
  try {
    const response = await request(appId, activityVersion, appCreatedAt)
    if (!response?.ok) {
      throw new Error(`activity acknowledgement failed (${response?.status ?? 'unknown'})`)
    }
    confirmSeen(appId, activityVersion)
    clearCached(appId, activityVersion)
    return true
  } catch {
    inFlight.delete(key)
    try {
      await restoreServerTruth()
    } catch {
      // Reconnect/foreground refresh remains the durable recovery path.
    }
    return false
  } finally {
    inFlight.delete(key)
  }
}
