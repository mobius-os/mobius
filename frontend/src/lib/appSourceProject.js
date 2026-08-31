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
