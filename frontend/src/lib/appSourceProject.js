const PREFIX = 'app-source:'

export function appSourceProjectId(appId) {
  return `${PREFIX}${String(appId)}`
}

export function parseAppSourceProjectId(projectId) {
  const value = String(projectId ?? '')
  if (!value.startsWith(PREFIX)) return null
  const appId = value.slice(PREFIX.length)
  return appId ? appId : null
}

// Only explicitly linked Projects can apply changes to an installed app.
export function linkedProjectAppId(project) {
  const imported = project?.template?.imported_from
  if (imported?.management !== 'linked' || imported.kind !== 'app' || imported.id == null) return null
  return String(imported.id) || null
}

export function projectImportSource(sources, kind, id) {
  if (sources?.management !== 'linked') return null
  const rows = kind === 'app' ? sources?.apps : kind === 'artifact' ? sources?.artifacts : []
  return rows?.find(source => String(source.id) === String(id)) || null
}
