// One Projects action for source menus: reuse a managed project before offering import.
import { projectImportSource } from './appSourceProject.js'

export function projectSourceAction(projects, importSources, kind, id) {
  if (!['app', 'artifact'].includes(kind) || id == null) return null
  const project = projects.find(row => {
    const source = row.template?.imported_from
    return source?.kind === kind && String(source.id) === String(id)
  })
  if (project) return { label: 'Show in Projects', project }
  const source = projectImportSource(importSources, kind, id)
  return source ? { label: 'Import to Projects', source } : null
}
