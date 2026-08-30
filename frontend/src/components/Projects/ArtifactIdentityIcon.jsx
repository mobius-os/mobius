import { artifactVisualKind } from '../../lib/projectArtifacts.js'
import ProjectArtifactIcon from './ProjectArtifactIcon.jsx'
import './ArtifactIdentityIcon.css'

const TONES = {
  html: ['#3b82f6', '#1d4ed8'],
  pdf: ['#a855f7', '#6b21a8'],
  image: ['#f97316', '#c2410c'],
  sheet: ['#22c55e', '#047857'],
  document: ['#14b8a6', '#0f766e'],
  visualization: ['#f97316', '#c2410c'],
  'mini-app': ['#ec4899', '#a21caf'],
}

export default function ArtifactIdentityIcon({ artifact, size = 30, className = '', label = '' }) {
  const kind = artifactVisualKind(artifact)
  const [primary, secondary] = TONES[kind] || TONES.html
  return (
    <span
      className={`artifact-identity-icon ${className}`.trim()}
      data-kind={kind}
      role={label ? 'img' : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : 'true'}
      style={{
        '--artifact-icon-size': `${size}px`,
        '--artifact-icon-primary': primary,
        '--artifact-icon-secondary': secondary,
      }}
    >
      <ProjectArtifactIcon artifact={artifact} size={Math.max(12, Math.round(size * .48))} strokeWidth={1.9} />
    </span>
  )
}
