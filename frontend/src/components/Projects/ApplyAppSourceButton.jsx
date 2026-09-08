/* Build & update publishes saved app source through the existing atomic app apply operation. */
import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, jsonOrThrow } from '../../api/client.js'
import { appQueries, appSourceQueries } from '../../hooks/queries.js'

export default function ApplyAppSourceButton({ app, onError, className = 'project-workspace__collaborate' }) {
  const [applying, setApplying] = useState(false)
  const [updated, setUpdated] = useState(false)
  const activeRequest = useRef(false)
  const queryClient = useQueryClient()

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
    {updated && <span className="project-update-status" role="status">App updated</span>}</>
}
