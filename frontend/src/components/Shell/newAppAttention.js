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
export function withAppActivitySeen(apps, appId, seenThroughVersion = Infinity) {
  const id = Number(appId)
  const seenThrough = Number(seenThroughVersion)
  if (!Array.isArray(apps) || Number.isNaN(id)) return apps
  let changed = false
  const next = apps.map(app => {
    if (Number(app?.id) !== id || !app?.has_unseen_activity) return app
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

// Own one exact app/version acknowledgement. The key is released before a
// failed request restores server truth, so the resulting refetch can retry
// immediately while the app is still visible. A successful request clears the
// cache again because an unrelated in-flight refetch may have restored the
// just-acknowledged version after the initial optimistic update.
export async function acknowledgeAppActivity({
  appId,
  activityVersion,
  inFlight,
  request,
  clearCached,
  restoreServerTruth,
}) {
  const key = `${appId}:${activityVersion}`
  if (inFlight.has(key)) return false
  inFlight.add(key)
  clearCached(appId, activityVersion)
  try {
    const response = await request(appId, activityVersion)
    if (!response?.ok) {
      throw new Error(`activity acknowledgement failed (${response?.status ?? 'unknown'})`)
    }
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
