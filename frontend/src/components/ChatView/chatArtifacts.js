/* Chat-scoped projection of records owned by the installed Artifacts app. */

import { parseApiTimestamp } from '../../lib/relativeTime.js'

const ARTIFACT_ID_RE = /^[A-Za-z0-9_-]{1,64}$/

function recordFromEntry(entry) {
  const content = entry?.content
  if (content && typeof content === 'object') return content
  if (typeof content !== 'string') return null
  try {
    const parsed = JSON.parse(content)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function timestamp(value) {
  const parsed = parseApiTimestamp(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function appId(app) {
  if (!app || typeof app !== 'object' || Array.isArray(app)) return null
  const id = Number(app.id ?? app.app_id)
  return Number.isInteger(id) && id > 0 ? id : null
}

function appSlug(app) {
  if (!app || typeof app !== 'object' || Array.isArray(app)) return ''
  return String(app.slug || '').trim()
}

export function artifactRelatedToApps(record, apps) {
  const related = Array.isArray(record?.related_apps) ? record.related_apps : []
  const candidates = Array.isArray(apps) ? apps : []
  return related.some((app) => {
    const slug = appSlug(app)
    if (slug) return candidates.some(candidate => appSlug(candidate) === slug)
    const id = appId(app)
    return id !== null && candidates.some(candidate => appId(candidate) === id)
  })
}

export function artifactTouchForChat(record, chatId, relatedApps = []) {
  if (!record || typeof record !== 'object') return null
  if (!ARTIFACT_ID_RE.test(record.id || '')) return null
  const owner = String(chatId || '')
  if (!owner) return null
  const versionTouches = (Array.isArray(record.versions) ? record.versions : [])
    .filter(version => String(version?.chat_id || '') === owner)
  const hasOriginTouch = versionTouches.length > 0 || String(record.chat_id || '') === owner
  const hasRelatedApp = artifactRelatedToApps(record, relatedApps)
  if (!hasOriginTouch && !hasRelatedApp) {
    return null
  }
  const latestVersionTouch = versionTouches.reduce((latest, version) => (
    timestamp(version?.created_at) > timestamp(latest?.created_at)
      ? version
      : latest
  ), null)
  const touchedAt = latestVersionTouch?.created_at
    || (hasOriginTouch ? record.created_at : record.updated_at)
    || record.updated_at
    || record.created_at
    || ''
  return {
    id: record.id,
    title: String(record.title || 'Untitled artifact'),
    description: String(record.description || ''),
    touchedAt,
    version: Math.max(
      1,
      Number(latestVersionTouch?.v ?? record.current_version) || 1,
    ),
  }
}

export function artifactsTouchedByChat(records, chatId, relatedApps = []) {
  return (Array.isArray(records) ? records : [])
    .map(record => artifactTouchForChat(record, chatId, relatedApps))
    .filter(Boolean)
    .sort((left, right) => timestamp(right.touchedAt) - timestamp(left.touchedAt))
}

// The backend's chat-app relation is durable across editing chats. Project it
// into the shared picker, deduping a briefly stale/refetched row by app id.
export function appArtifactsFromBuiltApps(builtApps) {
  const seen = new Set()
  const items = []
  for (let index = (Array.isArray(builtApps) ? builtApps.length : 0) - 1;
    index >= 0; index -= 1) {
    const app = builtApps[index]
    const id = Number(app?.id)
    if (!Number.isInteger(id) || id <= 0 || seen.has(id)) continue
    seen.add(id)
    items.push(app)
  }
  return items
}

export function chatArtifactPickerItems(builtApps, artifacts) {
  const appItems = appArtifactsFromBuiltApps(builtApps).map(app => ({
    key: `app:${app.id}`,
    kind: 'app',
    id: Number(app.id),
    title: String(app.name || 'Untitled app'),
    touchedAt: app.chat_touched_at || app.updated_at || app.created_at || '',
    unseen: Boolean(app.has_unseen_chat_update),
    app,
  }))
  const documentItems = (Array.isArray(artifacts) ? artifacts : []).map(artifact => ({
    ...artifact,
    key: `artifact:${artifact.id}`,
    kind: 'artifact',
    title: String(artifact.title || 'Untitled artifact'),
  }))
  return [...appItems, ...documentItems]
    .sort((left, right) => timestamp(right.touchedAt) - timestamp(left.touchedAt))
}

export async function loadChatArtifacts(
  appId,
  chatId,
  { signal, request, relatedApps = [] } = {},
) {
  if (typeof request !== 'function') {
    throw new Error('Artifact loading requires a request function.')
  }
  const records = []
  let cursor = null
  do {
    const params = new URLSearchParams({ limit: '500', include_content: 'true' })
    if (cursor) params.set('cursor', cursor)
    const response = await request(
      `/storage/apps-list/${encodeURIComponent(appId)}/artifacts/?${params}`,
      { signal },
    )
    if (!response.ok) throw new Error(`Artifacts request failed (${response.status})`)
    const page = await response.json()
    for (const entry of Array.isArray(page?.entries) ? page.entries : []) {
      const record = recordFromEntry(entry)
      if (record) records.push(record)
    }
    cursor = page?.next_cursor || null
  } while (cursor)
  return artifactsTouchedByChat(records, chatId, relatedApps)
}
