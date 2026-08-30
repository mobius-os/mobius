import { projectIdentityTone } from '../../lib/projectTypes.js'
import ProjectTypeIcon from './ProjectTypeIcon.jsx'
import './ProjectIdentityIcon.css'

export { projectIdentityTone }

export default function ProjectIdentityIcon({ project, size = 34, className = '', label = '' }) {
  const tone = projectIdentityTone(project)
  const iconSize = Math.max(13, Math.round(size * .46))
  return (
    <span
      className={`project-identity-icon ${className}`.trim()}
      data-kind={tone.kind}
      role={label ? 'img' : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : 'true'}
      style={{
        '--project-icon-size': `${size}px`,
        '--project-icon-accent': tone.accent,
      }}
    >
      <ProjectTypeIcon value={project} size={iconSize} strokeWidth={1.75} />
    </span>
  )
}
