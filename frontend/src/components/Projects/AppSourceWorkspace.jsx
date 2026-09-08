import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import ArrowUpRight from 'lucide-react/dist/esm/icons/arrow-up-right.mjs'
import AppIcon from '../AppIcon.jsx'
import { api, jsonOrThrow } from '../../api/client.js'
import { appSourceQueries } from '../../hooks/queries.js'
import ApplyAppSourceButton from './ApplyAppSourceButton.jsx'
import ProjectFinder from './ProjectFinder.jsx'
import './Projects.css'

export default function AppSourceWorkspace({ app, onOpenApp, requiresApply = false }) {
  const [error, setError] = useState('')
  const appId = String(app.id)
  const queryClient = useQueryClient()
  const fileSource = useMemo(() => ({
    id: `app-source:${appId}`,
    readOnly: false,
    filesKey: path => appSourceQueries.keys.files(appId, path),
    gitStatusKey: () => appSourceQueries.keys.gitStatus(appId),
    gitDiffKey: path => appSourceQueries.keys.gitDiff(appId, path),
    files: (path, options) => api.apps.sourceFiles(appId, path, options),
    gitStatus: options => api.apps.sourceGitStatus(appId, options),
    gitDiff: (path, options) => api.apps.sourceGitDiff(appId, path, options),
    readFile: (path, options) => api.apps.readSourceFile(appId, path, options),
    writeFile: (path, content, expectedRevision) => (
      api.apps.writeSourceFile(appId, path, content, expectedRevision)
    ),
    writeBytes: (path, bytes, expectedRevision) => (
      api.apps.writeSourceBytes(appId, path, bytes, expectedRevision)
    ),
    createFolder: path => api.apps.createSourceFolder(appId, path),
    deleteFile: path => api.apps.deleteSourcePath(appId, path),
    move: payload => api.apps.moveSourcePath(appId, payload),
    invalidate: queryClient => appSourceQueries.invalidate(queryClient, appId),
  }), [appId])

  async function applySourceChange() {
    if (!app.source_dir) throw new Error('This app source cannot be applied.')
    try {
      await jsonOrThrow(await api.apps.applySource({
        source_dir: app.source_dir,
      }), 'App update failed:')
    } finally {
      await appSourceQueries.invalidate(queryClient, appId)
    }
  }

  return (
    <section className="project-workspace app-source-workspace" aria-label={`${app.name} source`}>
      <header className="app-source-workspace__bar">
        <div className="app-source-workspace__identity">
          <AppIcon item={app} label={app.name} className="app-source-workspace__icon" />
          <span><strong>{app.name}</strong><small>Source</small></span>
        </div>
        {requiresApply && <ApplyAppSourceButton app={app} onError={setError} className="app-source-workspace__open" />}
        <button type="button" className="app-source-workspace__open" onClick={onOpenApp}>
          Open app <ArrowUpRight size={14} aria-hidden="true" />
        </button>
      </header>
      {requiresApply && <p className="project-source-notice">Saved changes require Build & update app.</p>}
      {error && <p className="projects-error" role="alert">{error}</p>}
      <div className="project-workspace__view">
        <ProjectFinder
          projectId={fileSource.id}
          projectName={`${app.name} source`}
          sourceDescription="Open app to use the running version."
          fileSource={fileSource}
          onSourceChanged={requiresApply ? undefined : applySourceChange}
        />
      </div>
    </section>
  )
}
