import { useState } from 'react'
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
      'Project artifacts failed:',
    )),
    // The owner shell forwards build-status events into this cache; a
    // collaborator route has no such stream, so poll only while a build runs.
    refetchInterval: query => (
      (query.state.data || []).some(row => row.status === 'building') ? 900 : false
    ),
  })
  const artifacts = artifactsQuery.data || []

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
        <p className="projects-empty" role="status">Loading artifacts…</p>
      ) : artifactsQuery.isError ? (
        <div className="projects-empty" role="alert"><p>Artifacts are unavailable.</p><button type="button" onClick={() => artifactsQuery.refetch()}>Try again</button></div>
      ) : artifacts.length === 0 ? (
        !linkedApp && <p className="projects-empty project-artifacts__empty">Build a supported file from its actions, or ask a project chat to create something.</p>
      ) : (
        <div className="project-artifacts__list">
          {artifacts.map(artifact => {
            const pill = artifactStatusPill(artifact)
            const building = isBuilding(artifact) || busyId === artifact.id
            const name = artifact.name || artifact.id
            return (
              <div key={artifact.id} className="project-artifacts__row">
                <button type="button" className="project-artifacts__main" aria-label={`Open ${name}`} onClick={() => onOpen?.(artifact.id)}>
                  <ArtifactIdentityIcon artifact={artifact} size={30} />
                  <span className="project-artifacts__copy">
                    <strong>{name}</strong>
                    <small>{artifactTypeName(artifact)}{artifact.source ? ` · ${artifact.source}` : ''}</small>
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
