/* Pure lifecycle projection for chat-owned edits and their contributions. */

export const CHANGE_STAGES = ['unsorted', 'prepared', 'open', 'settled']

const PREPARED = new Set(['prepared', 'submitting'])
const OPEN = new Set(['draft', 'open', 'landing'])
const SETTLED = new Set(['merged', 'superseded', 'closed'])
const ACTIVE_WORK = new Set([
  'accepted', 'retrying', 'starting', 'running', 'resuming', 'paused',
])
const ATTENTION_WORK = new Set(['failed', 'needs_review', 'interrupted'])

export function contributionWorkState(work) {
  const status = String(work?.status || '')
  if (!work?.id || !status) return null
  if (ACTIVE_WORK.has(status)) return 'active'
  if (status === 'completed') return 'completed'
  if (ATTENTION_WORK.has(status)) return 'attention'
  if (status === 'stopped' || status === 'cancelled') return 'stopped'
  return null
}

function cleanPath(value) {
  if (typeof value !== 'string') return ''
  const normalized = value.trim().replaceAll('\\', '/').replace(/\/{2,}/g, '/')
  if (!normalized) return ''
  return normalized
}

export function contributionStage(record) {
  if (PREPARED.has(record?.status)) return 'prepared'
  if (OPEN.has(record?.status)) return 'open'
  if (SETTLED.has(record?.status)) return 'settled'
  return null
}

export function contributionNeedsAttention(record) {
  if (SETTLED.has(record?.status)) return false
  if (record?.status === 'submitting' && record?.successor === true) {
    return record?.review?.state === 'needs_refresh'
  }
  return record?.needs_attention === true
    || Boolean(String(record?.last_submit_error || '').trim())
    || record?.review?.state === 'needs_refresh'
}

export function contributionSourceFile(record, file) {
  const path = cleanPath(file)
  if (!path) return ''
  if (path.startsWith('/')) return path
  const root = cleanPath(record?.source_root)
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

function isTrackableSourcePath(value) {
  const path = cleanPath(value)
  if (!path || path === '/tmp' || path.startsWith('/tmp/')) return false
  if (
    path === '/data/contrib' || path.startsWith('/data/contrib/')
    || path === '/data/agent-scratch' || path.startsWith('/data/agent-scratch/')
    || path === '/data/chats' || path.startsWith('/data/chats/')
    || path === '/data/shared' || path.startsWith('/data/shared/')
    || path === '/data/db' || path.startsWith('/data/db/')
    || path === '/data/cli-auth' || path.startsWith('/data/cli-auth/')
    || /^\/data\/apps\/\d+(?:\/|$)/.test(path)
  ) return false
  return true
}

function trackableEntry(entry) {
  const files = (entry?.preview?.files || []).filter(
    file => isTrackableSourcePath(file?.path),
  )
  return files.length ? { ...entry, preview: { ...entry.preview, files } } : null
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

function settlementCoversEntry(settlement, entry) {
  const coveredAt = instant(settlement?.coverage_at)
  const editedAt = instant(entry?.ts)
  return coveredAt !== null && (editedAt === null || editedAt <= coveredAt)
}

function changeSource(path) {
  const clean = cleanPath(path)
  const app = clean.match(/^\/data\/apps\/([^/]+)(?:\/|$)/)
  if (app) return {
    id: `/data/apps/${app[1]}`,
    label: app[1].split('-').filter(Boolean)
      .map(part => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' '),
  }
  if (clean === '/data/platform' || clean.startsWith('/data/platform/')) {
    return { id: '/data/platform', label: 'Möbius' }
  }
  const parts = clean.split('/').filter(Boolean)
  const id = clean.startsWith('/') ? `/${parts.slice(0, 2).join('/')}` : parts[0] || 'other'
  return { id, label: parts.at(-2) || parts[0] || 'Other project' }
}

export function groupUnsortedFiles(files) {
  const groups = new Map()
  for (const file of Array.isArray(files) ? files : []) {
    const source = changeSource(file?.path)
    const group = groups.get(source.id) || { ...source, files: [] }
    group.files.push(file)
    groups.set(source.id, group)
  }
  return [...groups.values()].sort((left, right) => left.label.localeCompare(right.label))
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
  const safeEntries = (Array.isArray(entries) ? entries : [])
    .map(trackableEntry)
    .filter(Boolean)
  const records = Array.isArray(payload?.records) ? payload.records : []
  const settlements = Array.isArray(payload?.settlements) ? payload.settlements : []
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
    for (const settlement of settlements) {
      const path = cleanPath(settlement?.path)
      if (path && settlementCoversEntry(settlement, entry)) covered.add(path)
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

  const stages = { prepared: [], open: [], settled: [] }
  for (const record of records) {
    const stage = contributionStage(record)
    if (stage) stages[stage].push(record)
  }
  for (const settlement of settlements) {
    const path = cleanPath(settlement?.path)
    if (!path || !paths.has(path)) continue
    stages.settled.push({
      ...settlement,
      kind: 'local',
      path,
      status: 'local',
    })
  }
  for (const stage of Object.values(stages)) {
    stage.sort((left, right) => String(right?.updated_at || '')
      .localeCompare(String(left?.updated_at || '')))
  }

  const attention = records.filter(contributionNeedsAttention).length
  const submitting = records.filter(record => record?.status === 'submitting').length
  const counts = {
    unsorted: unsortedPaths.length,
    prepared: stages.prepared.length,
    open: stages.open.length,
    settled: stages.settled.length,
    attention,
    submitting,
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
    work: payload?.work || null,
    workHistoryCount: Number.isInteger(payload?.work_history_count)
      ? payload.work_history_count
      : 0,
    workState: contributionWorkState(payload?.work),
    hasWork: counts.files > 0 || records.length > 0,
    needsAction: counts.unsorted > 0 || counts.prepared > 0 || attention > 0,
    workflowRevision: [
      unsortedEntries
        .map(entry => `${entry.id}:${entryPaths(entry).join(',')}`)
        .sort()
        .join('|'),
      ...records.map(record => `${record.id}:${record.action_key || record.status || ''}`)
        .sort(),
    ].join('||'),
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
  const hasStages = overview?.stages && typeof overview.stages === 'object'
  const withoutAttention = stage => hasStages
    ? (overview.stages?.[stage] || []).filter(record => !contributionNeedsAttention(record)).length
    : Number(counts[stage] || 0)
  const working = Number(counts.unsorted || 0)
    + withoutAttention('open')
  const ready = withoutAttention('prepared')
  const needsYou = Number(counts.attention || 0)
  const done = withoutAttention('settled')
  const parts = []
  if (working > 0) parts.push(`${working} working`)
  if (ready > 0) parts.push(`${ready} ready`)
  if (needsYou > 0) parts.push(`${needsYou} needs you`)
  if (done > 0) parts.push(`${done} done`)
  if (parts.length > 0) return parts.join(' · ')
  if (overview?.workState === 'active') return 'Preparing in background'
  if (overview?.workState === 'attention') return 'Preparation needs you'
  return 'No changes from this chat yet'
}
