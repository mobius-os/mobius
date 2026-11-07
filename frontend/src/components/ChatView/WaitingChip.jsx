/* WaitingChip renders what this chat is still owed, above the composer: helpers, waits, and resource holds. */

import { memo, useEffect, useRef, useState } from 'react'
import { ChevronRight, Clock } from '@openai/apps-sdk-ui/components/Icon'
import WaitingCard from './WaitingCard.jsx'
import { HelperRow } from './HelperResultCard.jsx'
import { useCountdownNow } from './countdownClock.js'
import { useDisclosureState } from './disclosureState.js'
import {
  handoffGroups,
  handoffSummaryLabel,
  shouldSummarizeHandoffs,
  waitNeedsOwner,
} from './handoffSummary.js'
import {
  resourcePausePresentation,
  waitPresentation,
} from './waitingPresentation.js'

// Per chat, whether the reader opened the summarized list (session screen state).
const HANDOFF_LIST_DISCLOSURE = 'foot-handoffs'

// The exact check, hidden until asked for: commands are long and technical.
function WaitCommand({ command }) {
  const [shown, setShown] = useState(false)
  const textRef = useRef(null)
  // Above the composer the handoff stack scrolls inside itself; bring the
  // revealed command and the card's Stop action below it into view, never
  // past the command's first line. Only the stack moves, never the transcript.
  useEffect(() => {
    const text = textRef.current
    const stack = text?.closest('.chat__waits')
    const card = text?.closest('.chat__wait-card')
    if (!shown || !stack || !card) return
    const view = stack.getBoundingClientRect()
    const overflow = Math.min(
      card.getBoundingClientRect().bottom - view.bottom,
      text.getBoundingClientRect().top - view.top,
    )
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
  const now = useCountdownNow(!wait.delivery_pending)
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
      stateLabel={waitNeedsOwner(wait) ? 'Needs you' : 'Waiting'}
      // Recovery needs the owner, so it stays on the collapsed line; stopping
      // is a deliberate choice made from the details.
      action={recoveryAction}
      detailAction={cancellable && onCancel ? {
        label: cancelling ? 'Stopping…' : wait.delivery_pending ? 'Stop follow-up' : 'Stop waiting',
        onClick: cancel, disabled: cancelling || wait.can_withdraw === false, error: cancelError,
      } : null}
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
function HelperListRows({ helpers, chatId, onInternalNav, label = 'Helpers', unlisted = 0, automatic = false }) {
  return (
    <section className="chat__wait-card chat__helper-list" aria-label={label}>
      {helpers.map(helper => (
        <HelperRow key={helper.delegation_id} event={helper} chatId={chatId} onInternalNav={onInternalNav} />
      ))}
      <HandoffNotes unlisted={unlisted} automatic={automatic} />
    </section>
  )
}

// The chat re-renders on every streamed token and rebuilds the helper rows
// each time; dozens of rows only re-render when what they show changed.
const sameHelperRows = (before, after) => before.length === after.length && before.every((row, index) => {
  const next = after[index]
  return row.delegation_id === next.delegation_id && row.status === next.status
    && row.task_key === next.task_key && row.provider === next.provider
})
const HelperList = memo(HelperListRows, (before, after) => (
  before.chatId === after.chatId
  && before.onInternalNav === after.onInternalNav
  && before.label === after.label
  && before.unlisted === after.unlisted
  && before.automatic === after.automatic
  && sameHelperRows(before.helpers, after.helpers)
))

function HandoffNotes({ unlisted, automatic }) {
  return <>
    {unlisted > 0 && <p className="chat__helper-list-note">
      {`and ${unlisted} more ${unlisted === 1 ? 'helper' : 'helpers'}`}
    </p>}
    {automatic && <p className="chat__helper-list-note">This chat resumes when they finish</p>}
  </>
}

// Many handoffs fold into one line of calm counts; opening it lists every row
// grouped by who acts next, inside the foot's own scroll.
function HandoffSummary({ label, open, onToggle }) {
  return (
    <section className="chat__wait-card chat__handoff-summary" aria-label="In progress">
      <button type="button" className="chat__wait-summary" title={label}
        aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} in-progress work: ${label}`}
        onPointerDown={event => event.preventDefault()}
        onClick={onToggle}>
        <span className="chat__progress-identity" aria-hidden="true"><Clock width={14} height={14} /></span>
        <span className="chat__wait-text">{label}</span>
        <ChevronRight className="chat__panel-chevron" width={14} height={14} aria-hidden="true" />
      </button>
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
  const [listOpen, setListOpen] = useDisclosureState(chatId, HANDOFF_LIST_DISCLOSURE)
  const hasHelpers = helpers.length > 0 || unlistedHelpers > 0
  if (!waits.length && !hasHelpers && !resourcePause) return null

  const toggle = key => setExpandedKey(current => current === key ? null : key)
  const automatic = handoff?.kind === 'automatic'
  const waitCard = wait => (
    <WaitCard
      key={wait.id}
      wait={wait}
      expanded={expandedKey === wait.id}
      onToggle={() => toggle(wait.id)}
      onCancel={onCancel}
      onRevealRecovery={onRevealRecovery}
    />
  )
  const summarized = shouldSummarizeHandoffs({ helpers, waits, unlistedHelpers })
  return (
    <section className={`chat__waits chat__handoffs${summarized && listOpen ? ' chat__handoffs--open' : ''}`} aria-label="Handoffs">
      {resourcePause && (
        <ResourceCard
          resourcePause={resourcePause}
          handoff={handoff}
          onRevealRecovery={onRevealRecovery}
          expanded={expandedKey === 'resource'}
          onToggle={() => toggle('resource')}
        />
      )}
      {summarized ? <>
        <HandoffSummary
          label={handoffSummaryLabel({ helpers, waits, unlistedHelpers })}
          open={listOpen}
          onToggle={() => setListOpen(value => !value)}
        />
        {listOpen && handoffGroups({ helpers, waits }).map(group => (
          <div key={group.key} className="chat__handoff-group" role="group" aria-label={`${group.label}: ${group.count}`}>
            <p className="chat__handoff-group-label" aria-hidden="true">{`${group.label} · ${group.count}`}</p>
            {group.helpers.length > 0 && (
              <HelperList helpers={group.helpers} chatId={chatId} onInternalNav={onInternalNav} label={`${group.label} helpers`} />
            )}
            {group.waits.map(waitCard)}
          </div>
        ))}
        {listOpen && (unlistedHelpers > 0 || automatic) && (
          <section className="chat__wait-card chat__handoff-notes">
            <HandoffNotes unlisted={unlistedHelpers} automatic={automatic} />
          </section>
        )}
      </> : <>
        {hasHelpers && (
          <HelperList helpers={helpers} chatId={chatId} onInternalNav={onInternalNav}
            unlisted={unlistedHelpers} automatic={automatic} />
        )}
        {waits.map(waitCard)}
      </>}
    </section>
  )
}
