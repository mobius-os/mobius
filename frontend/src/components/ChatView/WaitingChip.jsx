/* WaitingChip renders every self-resuming handoff above the composer. The
   collapsed row stays glanceable; expansion explains ownership and cost. */

import { useState } from 'react'
import { ChevronDown, Clock, X } from '@openai/apps-sdk-ui/components/Icon'
import {
  helperPresentation,
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

// One shell for every handoff card: a Waiting tag, text + meta in the summary
// row, and `rows` ([{ label, value, primary }]) behind the expander. `children`
// is the slot for card-specific actions below the rows.
function HandoffCard({
  expanded,
  onToggle,
  ariaLabel,
  title,
  text,
  meta,
  rows,
  children,
}) {
  return (
    <div className={`chat__wait-card${expanded ? ' chat__wait-card--expanded' : ''}`}>
      <button
        type="button"
        className="chat__wait-summary"
        aria-expanded={expanded}
        aria-label={`${expanded ? 'Collapse' : 'Expand'} ${ariaLabel}`}
        title={title}
        onPointerDown={(event) => event.preventDefault()}
        onClick={onToggle}
      >
        <span className="chat__wait-text">
          <span className="chat__progress-identity" aria-hidden="true"><Clock width={14} height={14} /></span>
          Waiting · {text}
        </span>
        <span className="chat__wait-meta">{meta}</span>
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
            {rows.map(row => (
              <DetailRow key={row.label} label={row.label} primary={!!row.primary}>
                {row.value}
              </DetailRow>
            ))}
          </dl>
          {children}
        </div>
      )}
    </div>
  )
}

export function WaitCard({ wait, expanded, onToggle, onCancel }) {
  const presentation = waitPresentation(wait)
  return (
    <HandoffCard
      expanded={expanded}
      onToggle={onToggle}
      ariaLabel={`waiting details: ${presentation.condition}`}
      title={`${presentation.condition} — ${presentation.summary}`}
      text={presentation.condition}
      meta={presentation.summary}
      rows={[
        { label: 'Waiting for', value: presentation.condition, primary: true },
        { label: 'Condition owner', value: presentation.owner },
        { label: 'Checker', value: presentation.checker },
        { label: 'Activity', value: presentation.activity },
        { label: 'If it takes too long', value: presentation.timeout },
        { label: 'Agent usage', value: presentation.usage },
      ]}
    >
      <button
        type="button"
        className="chat__wait-cancel"
        onPointerDown={(event) => event.preventDefault()}
        onClick={() => onCancel?.(wait.id)}
      >
        <X width={14} height={14} aria-hidden="true" />
        Stop waiting
      </button>
    </HandoffCard>
  )
}

function HelperCard({ backgroundHelpers, expanded, onToggle }) {
  const presentation = helperPresentation(backgroundHelpers)
  return (
    <HandoffCard
      expanded={expanded}
      onToggle={onToggle}
      ariaLabel="helper waiting details"
      title={presentation.tasks.length ? presentation.tasks.join(', ') : undefined}
      text={presentation.summary}
      meta="resumes automatically"
      rows={[
        {
          label: 'Waiting on',
          value: presentation.tasks.length
            ? presentation.tasks.join(', ')
            : 'Background helper work',
        },
        { label: 'Owner', value: presentation.owner },
        { label: 'Wake-up', value: 'This chat resumes when they finish' },
        { label: 'Agent usage', value: presentation.usage },
      ]}
    />
  )
}

function ResourceCard({ resourcePause, expanded, onToggle }) {
  const presentation = resourcePausePresentation(resourcePause)
  return (
    <HandoffCard
      expanded={expanded}
      onToggle={onToggle}
      ariaLabel="resource waiting details"
      title={`${presentation.summary} — ${presentation.next}`}
      text={presentation.summary}
      meta={presentation.next}
      rows={[
        { label: 'Waiting on', value: presentation.pressure },
        { label: 'Owner', value: presentation.owner },
        { label: 'Wake-up', value: presentation.wakeUp },
        { label: 'Agent usage', value: presentation.usage },
      ]}
    />
  )
}

export default function WaitingChip({
  waits = [],
  backgroundHelpers,
  resourcePause,
  onCancel,
}) {
  const helperCount = Number(backgroundHelpers?.count) || 0
  const [expandedKey, setExpandedKey] = useState(null)
  if (!waits.length && helperCount === 0 && !resourcePause) return null

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
      {helperCount > 0 && (
        <HelperCard
          backgroundHelpers={backgroundHelpers}
          expanded={expandedKey === 'helpers'}
          onToggle={() => toggle('helpers')}
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
