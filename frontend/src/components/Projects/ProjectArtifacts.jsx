import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, SettingsWrench as Hammer, EditPencil as Pencil } from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import { projectQueries } from '../../hooks/queries.js'
import {
  artifactStatusPill,
  artifactTypeName,
  isBuilding,
  normalizeArtifacts,
} from '../../lib/projectArtifacts.js'
import AppIcon from '../AppIcon.jsx'
import ApplyAppSourceButton from './ApplyAppSourceButton.jsx'
import ArtifactIdentityIcon from './ArtifactIdentityIcon.jsx'
import './Projects.css'

// The Artifacts zone: a project's buildable outputs. The project owns the
// source and the build; the artifact itself opens in its own independent
// viewer, so a row's main action is "open" and its small, always-visible
// action set is build/rebuild and jump-to-source. Artifacts are created for
// you — a templated project comes with one predefined, and any file can be
// built into one from its menu in the finder — so there is no manual "new
// artifact" form here.
export default function ProjectArtifacts({ projectId, onOpen, onEditSource, canBuild = true, linkedApp, onOpenApp }) {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState('')
  const artifactsQuery = useQuery({
    queryKey: projectQueries.keys.artifacts(projectId),
    queryFn: async ({ signal }) => normalizeArtifacts(await jsonOrThrow(
      await api.projects.artifacts(projectId, { signal }),
      'Project results failed:',
    )),
    // The owner shell forwards build-status events into this cache; a
    // collaborator route has no such stream, so poll only while a build runs.
    refetchInterval: query => (
      (query.state.data || []).some(row => row.status === 'building') ? 900 : false
    ),
  })
  const artifacts = artifactsQuery.data || []

  // A build this view watched finish earns "PDF ready · Open" until the result
  // is opened or built again. Builds started by a save or an agent count too.
  // `watchingIds` holds what was building at the last commit, so the render
  // that first sees the finished build already says ready, with no "Built" flash.
  const [readyIds, setReadyIds] = useState(() => new Set())
  const [watchingIds, setWatchingIds] = useState(() => new Set())
  useEffect(() => {
    const rows = artifactsQuery.data || []
    const building = new Set(rows
      .filter(artifact => isBuilding(artifact) || busyId === artifact.id)
      .map(artifact => artifact.id))
    const finished = rows
      .filter(artifact => watchingIds.has(artifact.id) && !building.has(artifact.id) && artifact.status === 'ok')
      .map(artifact => artifact.id)
    if (!sameIds(watchingIds, building)) setWatchingIds(building)
    setReadyIds(current => {
      const next = new Set([...current].filter(id => !building.has(id)))
      for (const id of finished) next.add(id)
      return sameIds(current, next) ? current : next
    })
  }, [artifactsQuery.data, busyId, watchingIds])

  function openResult(artifactId) {
    setReadyIds(current => {
      if (!current.has(artifactId)) return current
      const next = new Set(current)
      next.delete(artifactId)
      return next
    })
    onOpen?.(artifactId)
  }

  async function build(artifact) {
    if (busyId || isBuilding(artifact)) return
    setBusyId(artifact.id); setError('')
    try {
      await jsonOrThrow(await api.projects.buildArtifact(projectId, artifact.id), 'Build failed:')
      await queryClient.invalidateQueries({ queryKey: projectQueries.keys.artifacts(projectId) })
    } catch (cause) {
      setError(cause?.message || 'Could not start the build.')
    } finally { setBusyId('') }
  }

  return (
    <div className="project-artifacts">
      {error && <p className="projects-error" role="alert">{error}</p>}
      {linkedApp && <div className="project-artifacts__row project-artifacts__row--app">
        <button type="button" className="project-artifacts__main" aria-label={`Open running ${linkedApp.name}`} onClick={onOpenApp}>
          <AppIcon item={linkedApp} label={linkedApp.name} className="project-artifacts__app-icon" />
          <span className="project-artifacts__copy"><strong>{linkedApp.name}</strong></span>
          <span className="artifact-pill">Open app</span><ChevronRight width={16} height={16} aria-hidden="true" />
        </button>
        {canBuild && <div className="project-artifacts__build"><ApplyAppSourceButton projectId={projectId} app={linkedApp} onError={setError} className="project-artifacts__action" /></div>}
      </div>}
      {artifactsQuery.isLoading ? (
        <p className="projects-empty" role="status">Loading results…</p>
      ) : artifactsQuery.isError ? (
        <div className="projects-empty" role="alert"><p>Results are unavailable.</p><button type="button" onClick={() => artifactsQuery.refetch()}>Try again</button></div>
      ) : artifacts.length === 0 ? (
        !linkedApp && <p className="projects-empty project-artifacts__empty">Build a supported file from its actions, or ask a project chat to create something.</p>
      ) : (
        <div className="project-artifacts__list">
          {artifacts.map(artifact => {
            const building = isBuilding(artifact) || busyId === artifact.id
            const justBuilt = readyIds.has(artifact.id)
              || (watchingIds.has(artifact.id) && !building && artifact.status === 'ok')
            const pill = artifactStatusPill(artifact, { building, justBuilt })
            const name = artifact.name || artifact.id
            const typeName = artifactTypeName(artifact)
            const detail = [typeName === name ? '' : typeName, artifact.source].filter(Boolean).join(' · ')
            return (
              <div key={artifact.id} className="project-artifacts__row">
                <button type="button" className="project-artifacts__main" aria-label={`Open ${name}`} onClick={() => openResult(artifact.id)}>
                  <ArtifactIdentityIcon artifact={artifact} size={30} />
                  <span className="project-artifacts__copy">
                    <strong>{name}</strong>
                    {detail && <small>{detail}</small>}
                  </span>
                  <span className={`artifact-pill artifact-pill--${pill.variant}`}>{pill.label}</span>
                  <ChevronRight width={16} height={16} aria-hidden="true" className="project-artifacts__chevron" />
                </button>
                {onEditSource && artifact.source && !artifact.source_missing && (
                  <button type="button" className="project-artifacts__action" aria-label={`Edit source of ${name}`} title="Edit source" onClick={() => onEditSource(artifact.source)}><Pencil width={15} height={15} aria-hidden="true" /><span>Edit source</span></button>
                )}
                {canBuild && (
                  <button
                    type="button"
                    className="project-artifacts__action"
                    aria-label={`${artifact.has_output ? 'Rebuild' : 'Build'} ${name}`}
                    title={building ? 'Building…' : artifact.has_output ? 'Rebuild' : 'Build'}
                    disabled={building || artifact.source_missing}
                    onClick={() => void build(artifact)}
                  ><Hammer width={15} height={15} aria-hidden="true" className={building ? 'project-artifacts__action-icon--busy' : undefined} /><span>{building ? 'Building…' : artifact.has_output ? 'Rebuild' : 'Build'}</span></button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function sameIds(a, b) {
  return a.size === b.size && [...a].every(id => b.has(id))
}
