/* Chat-scoped projection of records owned by the installed Artifacts app. */

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
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function artifactTouchForChat(record, chatId) {
  if (!record || typeof record !== 'object') return null
  if (!ARTIFACT_ID_RE.test(record.id || '')) return null
  const owner = String(chatId || '')
  if (!owner) return null
  const versionTouches = (Array.isArray(record.versions) ? record.versions : [])
    .filter(version => String(version?.chat_id || '') === owner)
  if (versionTouches.length === 0 && String(record.chat_id || '') !== owner) {
    return null
  }
  const latestVersionTouch = versionTouches.reduce((latest, version) => (
    timestamp(version?.created_at) > timestamp(latest?.created_at)
      ? version
      : latest
  ), null)
  const touchedAt = latestVersionTouch?.created_at
    || record.created_at
    || record.updated_at
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

export function artifactsTouchedByChat(records, chatId) {
  return (Array.isArray(records) ? records : [])
    .map(record => artifactTouchForChat(record, chatId))
    .filter(Boolean)
    .sort((left, right) => timestamp(right.touchedAt) - timestamp(left.touchedAt))
}

export async function loadChatArtifacts(appId, chatId, { signal, request } = {}) {
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
  return artifactsTouchedByChat(records, chatId)
}
