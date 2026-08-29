/* Pure lifecycle projection for chat-owned edits and their contributions. */

export const CHANGE_STAGES = ['unsorted', 'prepared', 'open', 'landed']

const PREPARED = new Set(['prepared', 'submitting'])
const OPEN = new Set(['draft', 'open', 'landing'])
const LANDED = new Set(['merged', 'superseded', 'closed'])

function cleanPath(value) {
  if (typeof value !== 'string') return ''
  const normalized = value.trim().replaceAll('\\', '/').replace(/\/{2,}/g, '/')
  if (!normalized) return ''
  if (normalized.startsWith('a/')) return normalized.slice(2)
  if (normalized.startsWith('b/')) return normalized.slice(2)
  return normalized
}

function fallbackSourceRoot(record) {
  const repo = String(record?.repo || '').toLowerCase()
  if (repo === 'mobius-os/mobius') return '/data/platform'
  if (repo.startsWith('mobius-os/app-')) {
    return `/data/apps/${repo.slice('mobius-os/app-'.length)}`
  }
  return ''
}

export function contributionStage(record) {
  if (PREPARED.has(record?.status)) return 'prepared'
  if (OPEN.has(record?.status)) return 'open'
  if (LANDED.has(record?.status)) return 'landed'
  return null
}

export function contributionNeedsAttention(record) {
  return record?.needs_attention === true
    || Boolean(String(record?.last_submit_error || '').trim())
    || record?.review?.state === 'needs_refresh'
}

export function contributionSourceFile(record, file) {
  const path = cleanPath(file)
  if (!path) return ''
  if (path.startsWith('/')) return path
  const root = cleanPath(record?.source_root) || fallbackSourceRoot(record)
  return root ? cleanPath(`${root}/${path}`) : ''
}

function entryPaths(entry) {
  return (entry?.preview?.files || [])
    .map(file => cleanPath(file?.path))
    .filter(Boolean)
}

export function chatEditPaths(entries) {
  return [...new Set((Array.isArray(entries) ? entries : []).flatMap(entryPaths))]
    .sort()
}

function instant(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  const parsed = Date.parse(String(value || ''))
  return Number.isFinite(parsed) ? parsed : null
}

function recordCoversEntry(record, entry) {
  const coveredAt = instant(record?.coverage_at)
  const editedAt = instant(entry?.ts)
  return coveredAt !== null && (editedAt === null || editedAt <= coveredAt)
}

function exactCoverageByPath(payload) {
  if (!Array.isArray(payload?.coverage)) return null
  const coverage = new Map()
  for (const item of payload.coverage) {
    const path = cleanPath(item?.path)
    const coveredAt = instant(item?.coverage_at)
    if (!path || coveredAt === null) continue
    const previous = coverage.get(path)
    if (previous === undefined || coveredAt > previous) coverage.set(path, coveredAt)
  }
  return coverage
}

function exactCoverageCoversEntry(coverage, path, entry) {
  const coveredAt = coverage.get(path)
  if (coveredAt === undefined) return false
  const editedAt = instant(entry?.ts)
  return editedAt === null || editedAt <= coveredAt
}

function combinedFileStatus(previous, next) {
  if (next === 'D') return 'D'
  if (previous === 'A') return 'A'
  return next || previous || 'M'
}

function combineFiles(entries, allowedPaths) {
  const combined = new Map()
  for (const entry of entries) {
    for (const file of entry?.preview?.files || []) {
      const path = cleanPath(file?.path)
      if (!path || !allowedPaths.has(path)) continue
      const current = combined.get(path)
      if (!current) {
        combined.set(path, {
          ...file,
          path,
          hunks: [...(file?.hunks || [])],
        })
        continue
      }
      combined.set(path, {
        ...current,
        ...file,
        path,
        status: combinedFileStatus(current.status, file?.status),
        insertions: (current.insertions || 0) + (file?.insertions || 0),
        deletions: (current.deletions || 0) + (file?.deletions || 0),
        hunks: [...(current.hunks || []), ...(file?.hunks || [])],
      })
    }
  }
  return [...combined.values()].sort((left, right) => left.path.localeCompare(right.path))
}

export function chatChangesOverview(entries, payload) {
  const safeEntries = Array.isArray(entries) ? entries : []
  const records = Array.isArray(payload?.records) ? payload.records : []
  const paths = new Set(safeEntries.flatMap(entryPaths))
  const exactCoverage = exactCoverageByPath(payload)
  const coveredForEntry = new Map()
  for (const entry of safeEntries) {
    const covered = new Set()
    if (exactCoverage !== null) {
      for (const path of entryPaths(entry)) {
        if (exactCoverageCoversEntry(exactCoverage, path, entry)) {
          covered.add(path)
        }
      }
    } else {
      for (const record of records) {
        if (!recordCoversEntry(record, entry)) continue
        for (const file of record?.files || []) {
          const path = contributionSourceFile(record, file)
          if (path) covered.add(path)
        }
      }
    }
    coveredForEntry.set(entry, covered)
  }

  const unsortedEntries = safeEntries.flatMap(entry => {
    const covered = coveredForEntry.get(entry) || new Set()
    const files = (entry?.preview?.files || []).filter(
      file => !covered.has(cleanPath(file?.path)),
    )
    return files.length ? [{
      ...entry,
      preview: { ...entry.preview, files },
    }] : []
  })
  const unsortedPaths = [...new Set(unsortedEntries.flatMap(entryPaths))].sort()
  const unsortedSet = new Set(unsortedPaths)
  const unsortedFiles = combineFiles(unsortedEntries, unsortedSet)

  const stages = { prepared: [], open: [], landed: [] }
  for (const record of records) {
    const stage = contributionStage(record)
    if (stage) stages[stage].push(record)
  }
  for (const stage of Object.values(stages)) {
    stage.sort((left, right) => String(right?.updated_at || '')
      .localeCompare(String(left?.updated_at || '')))
  }

  const attention = records.filter(contributionNeedsAttention).length
  const counts = {
    unsorted: unsortedPaths.length,
    prepared: stages.prepared.length,
    open: stages.open.length,
    landed: stages.landed.length,
    attention,
    files: paths.size,
    updates: safeEntries.length,
  }
  return {
    entries: safeEntries,
    unsortedEntries,
    unsortedFiles,
    unsortedPaths,
    stages,
    counts,
    hasWork: counts.files > 0 || records.length > 0,
    needsAction: counts.unsorted > 0 || counts.prepared > 0 || attention > 0,
    unsortedRevision: unsortedEntries
      .map(entry => `${entry.id}:${entryPaths(entry).join(',')}`)
      .sort()
      .join('|'),
  }
}

export function compactChangesSummary(overview) {
  const counts = overview?.counts || {}
  if (overview?.lifecycleAvailable === false) {
    if (counts.files > 0) {
      return `${counts.files} recorded ${counts.files === 1 ? 'file' : 'files'} · status unavailable`
    }
    return 'Contribution status unavailable'
  }
  const parts = []
  if (counts.unsorted > 0) parts.push(`${counts.unsorted} unsorted`)
  if (counts.prepared > 0) parts.push(`${counts.prepared} prepared`)
  if (counts.open > 0) parts.push(`${counts.open} open`)
  if (counts.attention > 0) parts.push(`${counts.attention} need attention`)
  if (parts.length > 0) return parts.join(' · ')
  if (counts.landed > 0) return `${counts.landed} landed · everything settled`
  return 'No changes from this chat yet'
}

export function initialChangesStage(overview) {
  const counts = overview?.counts || {}
  if (counts.unsorted > 0) return 'unsorted'
  if (counts.prepared > 0) return 'prepared'
  if (counts.open > 0) return 'open'
  if (counts.landed > 0) return 'landed'
  return 'unsorted'
}

function revisionHash(value) {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

export function unsortedDismissKey(chatId, revision) {
  if (!chatId || !revision) return null
  return `mobius:changes-dismissed:${chatId}:${revisionHash(revision)}`
}

export function isUnsortedDismissed(chatId, revision, storage) {
  const key = unsortedDismissKey(chatId, revision)
  if (!key || !storage) return false
  try { return storage.getItem(key) === '1' } catch { return false }
}

export function rememberUnsortedDismissed(chatId, revision, storage) {
  const key = unsortedDismissKey(chatId, revision)
  if (!key || !storage) return false
  try {
    storage.setItem(key, '1')
    return true
  } catch {
    return false
  }
}
