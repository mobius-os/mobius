/* Pure projection of one chat's recorded source edits, grouped for reading. */

function cleanPath(value) {
  if (typeof value !== 'string') return ''
  const normalized = value.trim().replaceAll('\\', '/').replace(/\/{2,}/g, '/')
  if (!normalized) return ''
  if (normalized.startsWith('a/')) return normalized.slice(2)
  if (normalized.startsWith('b/')) return normalized.slice(2)
  return normalized
}

// Changes is a source view, not a second view of arbitrary transcript edits.
// Project workspaces have UUID roots; installed app source has a named slug.
function isSourcePath(value) {
  const path = cleanPath(value)
  if (!path || path.split('/').some(part => part === '.' || part === '..')) return false
  return path.startsWith('/data/platform/')
    || /^\/data\/apps\/[A-Za-z0-9_.-]*[A-Za-z_.-][A-Za-z0-9_.-]*\/.+/.test(path)
    || /^\/data\/projects\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/.+/.test(path)
}

function sourceEntry(entry) {
  const files = (entry?.preview?.files || []).filter(file => (
    isSourcePath(file?.path)
    && [file?.oldPath, file?.newPath].every(path => path == null || isSourcePath(path))
  ))
  return files.length ? { ...entry, preview: { ...entry.preview, files } } : null
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
  const project = clean.match(/^\/data\/projects\/([^/]+)\//)
  if (project) return { id: `/data/projects/${project[1]}`, label: 'Project' }
  const parts = clean.split('/').filter(Boolean)
  const id = clean.startsWith('/') ? `/${parts.slice(0, 2).join('/')}` : parts[0] || 'other'
  return { id, label: parts.at(-2) || parts[0] || 'Other project' }
}

export function groupChangedFiles(files) {
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

// Every edit to one path becomes one file with all of its hunks in order.
function combineFiles(entries) {
  const combined = new Map()
  for (const entry of entries) {
    for (const file of entry?.preview?.files || []) {
      const path = cleanPath(file?.path)
      if (!path) continue
      const current = combined.get(path)
      combined.set(path, current ? {
        ...current,
        ...file,
        path,
        status: combinedFileStatus(current.status, file?.status),
        insertions: (current.insertions || 0) + (file?.insertions || 0),
        deletions: (current.deletions || 0) + (file?.deletions || 0),
        hunks: [...(current.hunks || []), ...(file?.hunks || [])],
      } : { ...file, path, hunks: [...(file?.hunks || [])] })
    }
  }
  return [...combined.values()].sort((left, right) => left.path.localeCompare(right.path))
}

export function chatChanges(entries) {
  const sourceEntries = (Array.isArray(entries) ? entries : [])
    .map(sourceEntry)
    .filter(Boolean)
  const files = combineFiles(sourceEntries)
  return {
    entries: sourceEntries,
    files,
    groups: groupChangedFiles(files),
    excerptCount: sourceEntries.filter(entry => entry.preview?.truncated).length,
    latestTs: sourceEntries.reduce((latest, entry) => (
      typeof entry?.ts === 'number' && entry.ts > latest ? entry.ts : latest
    ), 0),
  }
}

export function compactChangesSummary(changes) {
  const count = changes?.files?.length || 0
  if (count === 0) return 'No file changes yet'
  return `${count} ${count === 1 ? 'file' : 'files'} changed`
}
