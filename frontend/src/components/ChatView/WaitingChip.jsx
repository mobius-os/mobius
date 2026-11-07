/* WaitingChip renders what this chat is still owed, above the composer: helpers, waits, and resource holds. */

import { useEffect, useRef, useState } from 'react'
import WaitingCard from './WaitingCard.jsx'
import { HelperRow } from './HelperResultCard.jsx'
import {
  resourcePausePresentation,
  waitPresentation,
} from './waitingPresentation.js'

// Countdowns read in whole minutes; a quarter-minute tick keeps them honest
// without re-rendering the foot every second.
const COUNTDOWN_TICK_MS = 15_000

function useCountdownClock(active) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return undefined
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), COUNTDOWN_TICK_MS)
    return () => clearInterval(id)
  }, [active])
  return now
}

// The exact check, hidden until asked for: commands are long and technical.
function WaitCommand({ command }) {
  const [shown, setShown] = useState(false)
  const textRef = useRef(null)
  // Above the composer the handoff stack scrolls inside itself; bring the
  // revealed command into that stack's view. Only the stack moves, never the
  // transcript.
  useEffect(() => {
    const text = textRef.current
    const stack = text?.closest('.chat__waits')
    if (!shown || !stack) return
    const overflow = text.getBoundingClientRect().bottom - stack.getBoundingClientRect().bottom
    if (overflow > 0) stack.scrollTop += overflow
  }, [shown])
  return <div className="chat__wait-command">
    <button type="button" className="chat__wait-command-toggle" aria-expanded={shown}
      onClick={() => setShown(value => !value)}>
      {shown ? 'Hide' : 'Show'}
    </button>
    {shown && <pre ref={textRef} className="chat__wait-command-text"><code>{command}</code></pre>}
  </div>
}

export function WaitCard({ wait, expanded, onToggle, onCancel, onRevealRecovery }) {
  const now = useCountdownClock(!wait.delivery_pending)
  const presentation = waitPresentation(wait, now)
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState(null)
  const cancel = async () => {
    setCancelling(true)
    setCancelError(null)
    try {
      const result = await onCancel(wait.id)
      if (result.disposition === 'not_withdrawable') setCancelError(result.detail)
    } catch {
      setCancelError('Could not stop this follow-up. Try again.')
    } finally {
      setCancelling(false)
    }
  }
  const needsRecovery = wait.delivery_pending && ['manual_resume', 'resume_failed', 'restart'].includes(wait.resume_blocker)
  const cancellable = wait.kind !== 'platform_activation' && !wait.result_disposition
    && (wait.delivery_pending || !wait.status || wait.status === 'armed')
    && (wait.can_withdraw !== false || !!cancelError)
  const recoveryAction = needsRecovery && onRevealRecovery
    ? { label: 'View recovery', onClick: onRevealRecovery } : null
  return (
    <WaitingCard
      expanded={expanded}
      onToggle={onToggle}
      ariaLabel="waiting details"
      title={`${presentation.condition} — ${presentation.summary}`}
      text={presentation.condition}
      meta={presentation.summary}
      stateLabel={wait.delivery_pending && ['manual_resume', 'resume_failed', 'owner_input', 'restart'].includes(wait.resume_blocker) ? 'Needs you' : 'Waiting'}
      action={cancellable && onCancel ? {
        label: cancelling ? 'Stopping…' : wait.delivery_pending ? 'Stop follow-up' : 'Stop waiting',
        onClick: cancel, disabled: cancelling || wait.can_withdraw === false, error: cancelError,
      } : recoveryAction}
      secondaryAction={cancellable && onCancel ? recoveryAction : null}
      rows={[
        { label: 'Waiting for', value: presentation.condition, primary: true },
        ...(wait.delivery_pending ? [{ label: 'Original condition', value: wait.description, primary: true }] : []),
        { label: 'Condition owner', value: presentation.owner },
        { label: 'Checker', value: presentation.checker },
        { label: 'Activity', value: presentation.activity },
        { label: presentation.timeoutLabel, value: presentation.timeout },
        { label: 'Agent usage', value: presentation.usage },
        ...(presentation.command ? [{ label: 'Command', value: <WaitCommand command={presentation.command} /> }] : []),
      ]}
    />
  )
}

// One row per helper, the same row the transcript shows at its launch, so a
// tap opens that helper's conversation. Status only: the live step and clock
// are not in this projection yet.
function HelperList({ helpers, unlisted, automatic, chatId, onInternalNav }) {
  return (
    <section className="chat__wait-card chat__helper-list" aria-label="Helpers">
      {helpers.map(helper => (
        <HelperRow key={helper.delegation_id} event={helper} chatId={chatId} onInternalNav={onInternalNav} />
      ))}
      {unlisted > 0 && <p className="chat__helper-list-note">
        {`and ${unlisted} more ${unlisted === 1 ? 'helper' : 'helpers'}`}
      </p>}
      {automatic && <p className="chat__helper-list-note">This chat resumes when they finish</p>}
    </section>
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
  helpers = [],
  unlistedHelpers = 0,
  resourcePause,
  handoff = null,
  chatId,
  onCancel,
  onRevealRecovery,
  onInternalNav,
}) {
  const [expandedKey, setExpandedKey] = useState(null)
  const hasHelpers = helpers.length > 0 || unlistedHelpers > 0
  if (!waits.length && !hasHelpers && !resourcePause) return null

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
      {hasHelpers && (
        <HelperList
          helpers={helpers}
          unlisted={unlistedHelpers}
          automatic={handoff?.kind === 'automatic'}
          chatId={chatId}
          onInternalNav={onInternalNav}
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
