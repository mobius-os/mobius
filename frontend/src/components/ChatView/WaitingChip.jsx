/* WaitingChip renders a chat's self-resuming handoffs above the composer. The
   collapsed row stays glanceable; expansion shows the full condition and who
   owns it without reviving the older background-helper rail. */

import { useState } from 'react'
import { ChevronDown, X } from '@openai/apps-sdk-ui/components/Icon'
import {
  resourcePausePresentation,
  waitPresentation,
} from './waitingPresentation.js'

function DetailRow({ label, children, primary = false }) {
  return (
    <div className={`chat__wait-detail-row${primary ? ' chat__wait-detail-row--primary' : ''}`}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function WaitTag() {
  return (
    <span className="chat__wait-tag" aria-hidden="true">
      <span className="chat__wait-pulse" />
      Waiting
    </span>
  )
}

export function WaitCard({ wait, expanded, onToggle, onCancel }) {
  const presentation = waitPresentation(wait)
  return (
    <div className={`chat__wait-card${expanded ? ' chat__wait-card--expanded' : ''}`}>
      <button
        type="button"
        className="chat__wait-summary"
        aria-expanded={expanded}
        aria-label={`${expanded ? 'Collapse' : 'Expand'} waiting details: ${wait.description}`}
        title={`${wait.description} — ${presentation.summary}`}
        onPointerDown={(event) => event.preventDefault()}
        onClick={onToggle}
      >
        <WaitTag />
        <span className="chat__wait-text">{wait.description}</span>
        <span className="chat__wait-meta">{presentation.summary}</span>
        <ChevronDown
          className="chat__wait-chevron"
          width={15}
          height={15}
          aria-hidden="true"
        />
      </button>
      {expanded && (
        <div className="chat__wait-details">
          <dl className="chat__wait-detail-list">
            <DetailRow label="Waiting for" primary>{presentation.condition}</DetailRow>
            <DetailRow label="Condition owner">{presentation.owner}</DetailRow>
            <DetailRow label="Checker">{presentation.checker}</DetailRow>
            <DetailRow label="Activity">{presentation.activity}</DetailRow>
            <DetailRow label="If it takes too long">{presentation.timeout}</DetailRow>
            <DetailRow label="Agent usage">{presentation.usage}</DetailRow>
          </dl>
          <button
            type="button"
            className="chat__wait-cancel"
            onPointerDown={(event) => event.preventDefault()}
            onClick={() => onCancel?.(wait.id)}
          >
            <X width={14} height={14} aria-hidden="true" />
            Stop waiting
          </button>
        </div>
      )}
    </div>
  )
}

function ResourceCard({ resourcePause, expanded, onToggle }) {
  const resourceLabel = resourcePause?.pause?.kind === 'memory'
    ? 'Waiting for memory headroom'
    : 'Waiting for storage headroom'
  const presentation = resourcePausePresentation(resourcePause, resourceLabel)
  return (
    <div className={`chat__wait-card${expanded ? ' chat__wait-card--expanded' : ''}`}>
      <button
        type="button"
        className="chat__wait-summary"
        aria-expanded={expanded}
        aria-label={`${expanded ? 'Collapse' : 'Expand'} resource waiting details`}
        title={`${presentation.summary} — ${presentation.next}`}
        onPointerDown={(event) => event.preventDefault()}
        onClick={onToggle}
      >
        <WaitTag />
        <span className="chat__wait-text">{presentation.summary}</span>
        <span className="chat__wait-meta">{presentation.next}</span>
        <ChevronDown
          className="chat__wait-chevron"
          width={15}
          height={15}
          aria-hidden="true"
        />
      </button>
      {expanded && (
        <div className="chat__wait-details">
          <dl className="chat__wait-detail-list">
            <DetailRow label="Waiting on">{presentation.pressure}</DetailRow>
            <DetailRow label="Owner">{presentation.owner}</DetailRow>
            <DetailRow label="Wake-up">{presentation.wakeUp}</DetailRow>
            <DetailRow label="Agent usage">{presentation.usage}</DetailRow>
          </dl>
        </div>
      )}
    </div>
  )
}

export default function WaitingChip({ waits = [], resourcePause, onCancel }) {
  const [expandedKey, setExpandedKey] = useState(null)
  if (!waits.length && !resourcePause) return null

  const toggle = key => setExpandedKey(current => current === key ? null : key)
  return (
    <section className="chat__waits" aria-label="Waiting handoffs">
      {resourcePause && (
        <ResourceCard
          resourcePause={resourcePause}
          expanded={expandedKey === 'resource'}
          onToggle={() => toggle('resource')}
        />
      )}
      {waits.map(wait => (
        <WaitCard
          key={wait.id}
          wait={wait}
          expanded={expandedKey === wait.id}
          onToggle={() => toggle(wait.id)}
          onCancel={onCancel}
        />
      ))}
    </section>
  )
}
