function requireRows(value, label) {
  if (!Array.isArray(value)) throw new Error(`${label} returned an invalid response.`)
  return value
}

function isAppRuntimeProject(project, appId) {
  if (!project || typeof project !== 'object') return false
  const template = project.template
  const importedByProjects = template
    && typeof template === 'object'
    && Object.hasOwn(template, 'imported_from')
  return !importedByProjects && String(project.source_app_id) === String(appId)
}

function appRuntimeProjectView(project) {
  return {
    id: typeof project.id === 'string' ? project.id : String(project.id ?? ''),
    name: typeof project.name === 'string' ? project.name : '',
    color: typeof project.color === 'string' ? project.color : null,
    project_type: typeof project.project_type === 'string' ? project.project_type : '',
    template: {
      id: typeof project.template?.id === 'string' ? project.template.id : '',
    },
    updated_at: typeof project.updated_at === 'string' ? project.updated_at : null,
  }
}

/**
 * Execute the Projects runtime contract for one attributed installed app.
 *
 * AppCanvas proves which app window sent the request. This controller keeps
 * the remaining ownership rule in one place for both the workspace and the
 * standalone host: apps may enumerate, migrate, create, and open only projects
 * and templates attributed to that same installed app.
 */
export async function handleAppProjectsRequest({
  app,
  client,
  readJson,
  browseProjects,
  openProject,
  publishProjects = () => {},
  onProjectCreated = () => {},
  randomUUID = () => globalThis.crypto.randomUUID(),
}, request) {
  if (app?.id == null) throw new Error('This app is no longer installed.')

  const readRows = async (response, label) => requireRows(
    await readJson(await response, `${label}:`),
    label,
  )
  const authorizedProjects = rows => rows.filter(
    row => isAppRuntimeProject(row, app.id),
  )
  const projectViews = rows => authorizedProjects(rows).map(appRuntimeProjectView)
  const readProjects = async () => {
    const rows = await readRows(client.list(), 'Project discovery failed')
    publishProjects(rows)
    return rows
  }

  if (request.action === 'browse') {
    browseProjects()
    return { opened: true }
  }

  if (request.action === 'migrate') {
    const legacyRows = await readRows(client.legacy(), 'Legacy project discovery failed')
    for (const legacy of legacyRows) {
      if (String(legacy.app_id) !== String(app.id) || legacy.imported) continue
      await readJson(await client.importLegacy({
        app_id: app.id,
        legacy_project_id: legacy.legacy_project_id,
        name: legacy.name,
      }), 'Legacy project import failed:')
    }
    return projectViews(await readProjects())
  }

  const projects = await readProjects()
  const ownedProjects = authorizedProjects(projects)
  if (request.action === 'list') return ownedProjects.map(appRuntimeProjectView)

  if (request.action === 'open') {
    const project = ownedProjects.find(row => String(row.id) === request.projectId)
    if (!project) throw new Error('That project is unavailable to this app.')
    openProject(project)
    return appRuntimeProjectView(project)
  }

  if (request.action !== 'create') throw new Error('Unsupported Projects request.')
  const templates = await readRows(client.templates(), 'Project templates failed')
  const template = templates.find(row => (
    String(row?.source_app_id) === String(app.id)
    && row.key === request.templateId
  ))
  if (!template) throw new Error('That project type is unavailable to this app.')
  const project = await readJson(await client.create({
    name: request.name || `Untitled ${template.name.toLowerCase()}`,
    template_id: template.key,
    recovery_request_id: randomUUID(),
  }), 'Project creation failed:')
  if (!isAppRuntimeProject(project, app.id)) {
    throw new Error('The created project is unavailable to this app.')
  }
  const updated = [
    project,
    ...projects.filter(row => String(row.id) !== String(project.id)),
  ]
  publishProjects(updated)
  onProjectCreated(project)
  openProject(project)
  return appRuntimeProjectView(project)
}
