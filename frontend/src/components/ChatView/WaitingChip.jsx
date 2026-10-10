/* WaitingChip renders bordered waits with their existing condition and recovery owners. */

import { useState } from 'react'
import WaitingCard from './WaitingCard.jsx'
import {
  helperPresentation,
  resourcePausePresentation,
  waitPresentation,
} from './waitingPresentation.js'

export function WaitCard({ wait, expanded, onToggle, onCancel, onRevealRecovery }) {
  const presentation = waitPresentation(wait)
  const needsRecovery = wait.delivery_pending && ['manual_resume', 'resume_failed', 'restart'].includes(wait.resume_blocker)
  const cancellable = wait.kind !== 'platform_activation' && !wait.delivery_pending
  return (
    <WaitingCard
      expanded={expanded}
      onToggle={onToggle}
      ariaLabel="waiting details"
      title={`${presentation.condition} — ${presentation.summary}`}
      text={presentation.condition}
      meta={presentation.summary}
      stateLabel={wait.delivery_pending && ['manual_resume', 'resume_failed', 'owner_input', 'restart'].includes(wait.resume_blocker) ? 'Needs you' : 'Waiting'}
      action={needsRecovery && onRevealRecovery
        ? { label: 'View recovery', onClick: onRevealRecovery }
        : cancellable && onCancel ? { label: 'Stop waiting', onClick: () => onCancel(wait.id) } : null}
      rows={[
        { label: 'Waiting for', value: presentation.condition, primary: true },
        ...(wait.delivery_pending ? [{ label: 'Original condition', value: wait.description, primary: true }] : []),
        { label: 'Condition owner', value: presentation.owner },
        { label: 'Checker', value: presentation.checker },
        { label: 'Activity', value: presentation.activity },
        { label: presentation.timeoutLabel, value: presentation.timeout },
        { label: 'Agent usage', value: presentation.usage },
      ]}
    />
  )
}

function HelperCard({ backgroundHelpers, handoff, expanded, onToggle }) {
  const presentation = helperPresentation(backgroundHelpers, handoff)
  return (
    <WaitingCard
      expanded={expanded}
      onToggle={onToggle}
      ariaLabel="helper waiting details"
      title={presentation.tasks.length ? presentation.tasks.join(', ') : undefined}
      text={presentation.summary}
      stateLabel={null}
      meta={presentation.automatic ? 'resumes automatically' : null}
      rows={[
        {
          label: 'Waiting on',
          value: presentation.tasks.length
            ? presentation.tasks.join(', ')
            : 'Background helper work',
        },
        { label: 'Owner', value: presentation.owner },
        { label: 'Next', value: presentation.automatic ? 'This chat resumes when they finish' : 'Waiting for the next step' },
        { label: 'Agent usage', value: presentation.usage },
      ]}
    />
  )
}

function ResourceCard({ resourcePause, handoff, expanded, onToggle, onRevealRecovery }) {
  const presentation = resourcePausePresentation(resourcePause, handoff)
  return (
    <WaitingCard
      expanded={expanded}
      onToggle={onToggle}
      ariaLabel="resource handoff details"
      action={presentation.manual && onRevealRecovery ? { label: 'View recovery', onClick: onRevealRecovery } : null}
      stateLabel={presentation.manual ? 'Needs you' : 'Waiting'}
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
  handoff = null,
  onCancel,
  onRevealRecovery,
}) {
  const helperCount = Number(backgroundHelpers?.count) || 0
  const [expandedKey, setExpandedKey] = useState(null)
  if (!waits.length && helperCount === 0 && !resourcePause) return null

  const toggle = key => setExpandedKey(current => current === key ? null : key)
  return (
    <section className="chat__waits" aria-label="Handoffs">
      {resourcePause && (
        <ResourceCard
          resourcePause={resourcePause}
          handoff={handoff}
          onRevealRecovery={onRevealRecovery}
          expanded={expandedKey === 'resource'}
          onToggle={() => toggle('resource')}
        />
      )}
      {helperCount > 0 && (
        <HelperCard
          backgroundHelpers={backgroundHelpers}
          handoff={handoff}
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
          onRevealRecovery={onRevealRecovery}
        />
      ))}
    </section>
  )
}
