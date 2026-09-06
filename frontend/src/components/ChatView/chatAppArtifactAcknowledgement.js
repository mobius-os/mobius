/* Exact-cursor acknowledgement for app updates visible in the Brain popover. */

export function unseenAppTouchCursors(apps) {
  const byId = new Map()
  for (const app of Array.isArray(apps) ? apps : []) {
    if (
      app?.id == null
      || !app.chat_touched_at
      || !app.has_unseen_chat_update
    ) continue
    byId.set(Number(app.id), {
      app_id: Number(app.id),
      touched_at: app.chat_touched_at,
    })
  }
  return [...byId.values()]
}

export function appTouchCursorsForBrainOpen(isOpen, apps) {
  return isOpen ? [] : unseenAppTouchCursors(apps)
}

export function acknowledgeChatArtifactRows(rows, touches) {
  if (!Array.isArray(rows) || rows.length === 0) return rows
  const touchedByApp = new Map(
    (Array.isArray(touches) ? touches : []).map(touch => [
      Number(touch?.app_id),
      touch?.touched_at,
    ]),
  )
  let changed = false
  const acknowledged = rows.map(row => {
    const expected = touchedByApp.get(Number(row?.app?.id))
    if (!expected || row?.touched_at !== expected || row?.seen_at === expected) {
      return row
    }
    changed = true
    return { ...row, seen_at: expected }
  })
  return changed ? acknowledged : rows
}
