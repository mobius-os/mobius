import { useQuery } from '@tanstack/react-query'
import ChevronRight from 'lucide-react/dist/esm/icons/chevron-right.mjs'
import { api, jsonOrThrow } from '../../api/client.js'
import { projectQueries } from '../../hooks/queries.js'
import {
  artifactStatusPill,
  artifactTypeName,
  normalizeArtifacts,
} from '../../lib/projectArtifacts.js'
import ArtifactIdentityIcon from './ArtifactIdentityIcon.jsx'
import './Projects.css'

// The Artifacts zone: a simple list of a project's buildable outputs, each
// opening in its own tab. Artifacts are created for you — a
// templated project comes with one predefined, and any file can be built into
// one from its ⋯ menu in the finder — so this panel deliberately has no manual
// "new artifact" form. Kept as plain as possible.
export default function ProjectArtifacts({ projectId, onOpen }) {
  const artifactsQuery = useQuery({
    queryKey: projectQueries.keys.artifacts(projectId),
    queryFn: async ({ signal }) => normalizeArtifacts(await jsonOrThrow(
      await api.projects.artifacts(projectId, { signal }),
      'Project artifacts failed:',
    )),
  })
  const artifacts = artifactsQuery.data || []

  return (
    <div className="project-artifacts">
      {artifactsQuery.isLoading ? (
        <p className="projects-empty" role="status">Loading artifacts…</p>
      ) : artifactsQuery.isError ? (
        <div className="projects-empty" role="alert"><p>Artifacts are unavailable.</p><button type="button" onClick={() => artifactsQuery.refetch()}>Try again</button></div>
      ) : artifacts.length === 0 ? (
        <p className="projects-empty project-artifacts__empty">No artifacts yet.</p>
      ) : (
        <div className="project-artifacts__list">
          {artifacts.map(artifact => {
            const pill = artifactStatusPill(artifact)
            return (
              <button key={artifact.id} type="button" className="project-artifacts__row" onClick={() => onOpen?.(artifact.id)}>
                <ArtifactIdentityIcon artifact={artifact} size={30} />
                <span className="project-artifacts__copy">
                  <strong>{artifact.name || artifact.id}</strong>
                  <small>{artifactTypeName(artifact)}{artifact.source ? ` · ${artifact.source}` : ''}</small>
                </span>
                <span className={`artifact-pill artifact-pill--${pill.variant}`}>{pill.label}</span>
                <ChevronRight size={16} aria-hidden="true" className="project-artifacts__chevron" />
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
