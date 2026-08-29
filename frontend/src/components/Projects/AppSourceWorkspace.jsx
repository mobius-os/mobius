import { useMemo } from 'react'
import ArrowUpRight from 'lucide-react/dist/esm/icons/arrow-up-right.mjs'
import AppIcon from '../AppIcon.jsx'
import { api } from '../../api/client.js'
import { appSourceQueries } from '../../hooks/queries.js'
import ProjectFinder from './ProjectFinder.jsx'
import './Projects.css'

export default function AppSourceWorkspace({ app, onOpenApp }) {
  const appId = String(app.id)
  const fileSource = useMemo(() => ({
    id: `app-source:${appId}`,
    readOnly: true,
    filesKey: path => appSourceQueries.keys.files(appId, path),
    gitStatusKey: () => appSourceQueries.keys.gitStatus(appId),
    gitDiffKey: path => appSourceQueries.keys.gitDiff(appId, path),
    files: (path, options) => api.apps.sourceFiles(appId, path, options),
    gitStatus: options => api.apps.sourceGitStatus(appId, options),
    gitDiff: (path, options) => api.apps.sourceGitDiff(appId, path, options),
    readFile: (path, options) => api.apps.readSourceFile(appId, path, options),
    invalidate: queryClient => appSourceQueries.invalidate(queryClient, appId),
  }), [appId])

  return (
    <section className="project-workspace app-source-workspace" aria-label={`${app.name} source`}>
      <header className="app-source-workspace__bar">
        <div className="app-source-workspace__identity">
          <AppIcon item={app} label={app.name} className="app-source-workspace__icon" />
          <span><strong>{app.name}</strong><small>Source</small></span>
        </div>
        <button type="button" className="app-source-workspace__open" onClick={onOpenApp}>
          Open app <ArrowUpRight size={14} aria-hidden="true" />
        </button>
      </header>
      <div className="project-workspace__view">
        <ProjectFinder
          projectId={fileSource.id}
          projectName={`${app.name} source`}
          fileSource={fileSource}
        />
      </div>
    </section>
  )
}
