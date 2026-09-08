/* Build & update publishes saved app source through the existing atomic app apply operation. */
import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, jsonOrThrow } from '../../api/client.js'
import { appQueries, appSourceQueries } from '../../hooks/queries.js'

export default function ApplyAppSourceButton({ app, projectId, onError, className = 'project-workspace__collaborate' }) {
  const [applying, setApplying] = useState(false)
  const [updated, setUpdated] = useState(false)
  const activeRequest = useRef(false)
  const queryClient = useQueryClient()
  const sourceStatus = useQuery({
    queryKey: ['projects', 'git', projectId, 'status'],
    queryFn: async () => jsonOrThrow(await api.projects.gitStatus(projectId), 'Build status failed:'),
    enabled: !!projectId,
    staleTime: 5_000,
  })

  async function apply() {
    if (activeRequest.current || !app?.source_dir) return
    activeRequest.current = true
    setApplying(true)
    onError?.('')
    setUpdated(false)
    try {
      await jsonOrThrow(await api.apps.applySource({ source_dir: app.source_dir }), 'App update failed:')
      setUpdated(true)
      await Promise.all([
        appQueries.list.invalidate(queryClient),
        appSourceQueries.invalidate(queryClient, app.id),
        ...(projectId ? [queryClient.invalidateQueries({ queryKey: ['projects', 'git', projectId] })] : []),
      ])
    } catch (error) {
      onError?.(error?.message || 'Could not apply this app. Your saved source is unchanged.')
    } finally {
      activeRequest.current = false
      setApplying(false)
    }
  }

  return <><button type="button" className={className} disabled={applying || !app?.source_dir}
    title="Build saved source and update the running app. Save your files first."
    onClick={() => void apply()}>{applying ? 'Building & updating…' : 'Build & update app'}</button>
    {sourceStatus.data?.app_build?.state === 'pending' && <span className="project-update-status" title="Saved source differs from the last app build">Build needed</span>}
    {updated && sourceStatus.data?.app_build?.state !== 'pending' && <span className="project-update-status" role="status">App updated</span>}</>
}
