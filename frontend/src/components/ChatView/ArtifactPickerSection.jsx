/* ArtifactPickerSection renders the chat's latest app and document previews. */

import AppIcon from '../AppIcon.jsx'
import { formatRelativeTime } from '../../lib/relativeTime.js'

function artifactSubtitle(item, latest = false) {
  const relativeTime = formatRelativeTime(item.touchedAt)
  if (item.kind === 'app') {
    return relativeTime ? `App · Updated ${relativeTime}` : 'App · Open preview'
  }
  if (latest) {
    return relativeTime
      ? `Edited here ${relativeTime} · v${item.version}`
      : `Edited in this chat · v${item.version}`
  }
  return relativeTime || `Version ${item.version}`
}

function ArtifactIcon({ artifact, documentIcon }) {
  return artifact.kind === 'app' ? (
    <AppIcon
      item={artifact.app}
      label={artifact.title}
      className="composer-popover__app-icon"
    />
  ) : (
    <span className="composer-popover__row-icon" aria-hidden="true">
      {documentIcon}
    </span>
  )
}

export default function ArtifactPickerSection({
  latestArtifact,
  otherArtifacts,
  expanded,
  onToggle,
  onOpenArtifact,
  documentIcon,
  disclosureIcon,
}) {
  const otherCount = otherArtifacts.length
  return (
    <div className="composer-popover__section composer-popover__section--artifacts">
      {otherCount > 0 ? (
        <button
          type="button"
          className={`composer-popover__artifact-heading${expanded ? ' composer-popover__artifact-heading--expanded' : ''}`}
          aria-expanded={expanded}
          aria-label={expanded
            ? 'Hide other artifacts'
            : `Show ${otherCount} more artifacts`}
          onClick={onToggle}
        >
          <span className="composer-popover__eyebrow">Latest artifacts</span>
          <span className="composer-popover__artifact-count" aria-hidden="true">
            {otherCount + 1}
          </span>
          {disclosureIcon}
        </button>
      ) : (
        <span className="composer-popover__eyebrow">Latest artifacts</span>
      )}
      <button
        type="button"
        className="composer-popover__row composer-popover__artifact-latest"
        onClick={() => onOpenArtifact(latestArtifact)}
      >
        <ArtifactIcon artifact={latestArtifact} documentIcon={documentIcon} />
        <span className="composer-popover__row-main">
          <span className="composer-popover__row-title">{latestArtifact.title}</span>
          <span className="composer-popover__row-sub">{artifactSubtitle(latestArtifact, true)}</span>
        </span>
      </button>
      {expanded && otherCount > 0 && (
        <div className="composer-popover__artifact-list">
          {otherArtifacts.map(artifact => (
            <button
              key={artifact.key}
              type="button"
              className="composer-popover__row"
              onClick={() => onOpenArtifact(artifact)}
            >
              <ArtifactIcon artifact={artifact} documentIcon={documentIcon} />
              <span className="composer-popover__row-main">
                <span className="composer-popover__row-title">{artifact.title}</span>
                <span className="composer-popover__row-sub">{artifactSubtitle(artifact)}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
