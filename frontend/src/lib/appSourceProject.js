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

export function appSourceProject(app) {
  if (app?.id == null) return null
  return {
    id: appSourceProjectId(app.id),
    name: `${app.name || 'App'} · Source`,
    source_kind: 'app',
    source_app_id: String(app.id),
    app,
  }
}

// Older clients implemented "import app" by copying the app into a Web Studio
// Project. Keep those owner snapshots intact, but project their workspace and
// artifact tabs onto the installed app that the import metadata identifies.
// New clients skip the copy entirely and open appSourceProject(app) directly.
export function importedAppId(project) {
  const imported = project?.template?.imported_from
  if (imported?.kind !== 'app' || imported.id == null) return null
  const id = String(imported.id)
  return id ? id : null
}

export function importedAppForProject(project, appById) {
  const appId = importedAppId(project)
  return appId ? appById.get(appId) || null : null
}

export function appForSourceImport(source, apps) {
  if (source?.kind !== 'app' || source.id == null || !Array.isArray(apps)) return null
  const appId = String(source.id)
  return apps.find(app => String(app?.id) === appId) || null
}
