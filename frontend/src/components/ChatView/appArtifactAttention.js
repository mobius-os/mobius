/* Pure attention state for app updates that land in the Brain artifact picker. */

import { parseApiTimestamp } from '../../lib/relativeTime.js'

function touchTime(value) {
  const parsed = parseApiTimestamp(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function appArtifactAttentionDecision(apps, lastTouches) {
  const previous = lastTouches instanceof Map ? lastTouches : new Map()
  const nextTouches = new Map()
  const dropApps = []
  for (const app of Array.isArray(apps) ? apps : []) {
    if (app?.id == null || !app.chat_touched_at) continue
    const id = Number(app.id)
    const touchedAt = app.chat_touched_at
    nextTouches.set(id, touchedAt)
    const advanced = !previous.has(id) || previous.get(id) !== touchedAt
    if (!advanced || !app.has_unseen_chat_update) continue
    dropApps.push(app)
  }
  dropApps.sort((left, right) => (
    touchTime(right.chat_touched_at) - touchTime(left.chat_touched_at)
    || Number(right.id) - Number(left.id)
  ))
  return {
    dropApps,
    nextTouches,
  }
}

export function appArtifactTouchKey(app) {
  if (app?.id == null || !app.chat_touched_at) return ''
  return `${Number(app.id)}:${app.chat_touched_at}`
}

export function unseenAppArtifactCount(apps) {
  return (Array.isArray(apps) ? apps : []).filter(
    app => app?.has_unseen_chat_update,
  ).length
}
