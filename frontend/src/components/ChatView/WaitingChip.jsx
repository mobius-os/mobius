/* WaitingChip renders what this chat is still owed, above the composer: helpers, waits, and resource holds. */

import { memo, useEffect, useRef, useState } from 'react'
import { ChevronRight, Clock } from '@openai/apps-sdk-ui/components/Icon'
import WaitingCard from './WaitingCard.jsx'
import { HelperEventConversation, HelperRowLine } from './HelperResultCard.jsx'
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
function WaitCommand({ command, shown, onToggle }) {
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
      onClick={onToggle}>
      {shown ? 'Hide' : 'Show'}
    </button>
    {shown && <pre ref={textRef} className="chat__wait-command-text"><code>{command}</code></pre>}
  </div>
}

const NO_STOP = { pending: false, error: null }

// `stop` is this wait's Stop request (pending, then why it was refused) and
// `commandShown` whether its details reveal the command; the stack keeps both
// (see WaitingChip), so the card itself holds no state a remount could lose.
export function WaitCard({
  wait, expanded, onToggle, onCancel, stop = NO_STOP, commandShown = false, onToggleCommand, onRevealRecovery,
}) {
  const now = useCountdownNow(!wait.delivery_pending)
  const presentation = waitPresentation(wait, now)
  const needsRecovery = wait.delivery_pending && ['manual_resume', 'resume_failed', 'restart'].includes(wait.resume_blocker)
  const cancellable = wait.kind !== 'platform_activation' && !wait.result_disposition
    && (wait.delivery_pending || !wait.status || wait.status === 'armed')
    && (wait.can_withdraw !== false || !!stop.error)
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
        label: stop.pending ? 'Stopping…' : wait.delivery_pending ? 'Stop follow-up' : 'Stop waiting',
        onClick: onCancel, disabled: stop.pending || wait.can_withdraw === false, error: stop.error,
      } : null}
      rows={[
        { label: 'Waiting for', value: presentation.condition, primary: true },
        ...(wait.delivery_pending ? [{ label: 'Original condition', value: wait.description, primary: true }] : []),
        { label: 'Condition owner', value: presentation.owner },
        { label: 'Checker', value: presentation.checker },
        { label: 'Activity', value: presentation.activity },
        { label: presentation.timeoutLabel, value: presentation.timeout },
        { label: 'Agent usage', value: presentation.usage },
        ...(presentation.command ? [{ label: 'Command', value: <WaitCommand command={presentation.command} shown={commandShown} onToggle={onToggleCommand} /> }] : []),
      ]}
    />
  )
}

// One row per helper, the same row the transcript shows at its launch, so a
// tap opens that helper's conversation. Status only: the live step and clock
// are not in this projection yet.
function HelperListRows({ helpers, chatId, onOpenHelper, label = 'Helpers', unlisted = 0, automatic = false }) {
  return (
    <section className="chat__wait-card chat__helper-list" aria-label={label}>
      {helpers.map(helper => (
        <HelperRowLine key={helper.delegation_id} event={helper} chatId={chatId}
          onOpen={event => onOpenHelper({ id: helper.delegation_id, host: event.currentTarget.closest('.chat') })} />
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
  && before.onOpenHelper === after.onOpenHelper
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

// A Stop request per wait id: pending while it runs, then the reason it was
// refused. A stop that succeeds removes the wait, and its entry with it.
function useWaitStops(onCancel) {
  const [stops, setStops] = useState({})
  const stopWait = async id => {
    setStops(current => ({ ...current, [id]: { pending: true, error: null } }))
    let error = null
    try {
      const result = await onCancel(id)
      if (result.disposition === 'not_withdrawable') error = result.detail
    } catch {
      error = 'Could not stop this follow-up. Try again.'
    }
    setStops(current => {
      const next = { ...current }
      delete next[id]
      if (error) next[id] = { pending: false, error }
      return next
    })
  }
  return [stops, stopWait]
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
  // Rows move to a new parent when the stack folds into its summary or a row
  // changes group, and React remounts a row whose parent changed. So what the
  // reader opened lives here, keyed by each row's own id: the expanded card
  // (with its revealed command, which collapsing hides), each wait's Stop
  // request, and the helper whose conversation is open.
  const [expanded, setExpanded] = useState({ key: null, command: false })
  const [stops, stopWait] = useWaitStops(onCancel)
  const [openHelper, setOpenHelper] = useState(null)
  const [listOpen, setListOpen] = useDisclosureState(chatId, HANDOFF_LIST_DISCLOSURE)
  const conversationHelper = openHelper && helpers.find(helper => helper.delegation_id === openHelper.id)
  // A helper that leaves the stack takes its conversation with it, and a
  // later return does not reopen it.
  if (openHelper && !conversationHelper) setOpenHelper(null)
  const hasHelpers = helpers.length > 0 || unlistedHelpers > 0
  if (!waits.length && !hasHelpers && !resourcePause) return null

  const toggle = key => setExpanded(current => ({ key: current.key === key ? null : key, command: false }))
  const automatic = handoff?.kind === 'automatic'
  const waitCard = wait => (
    <WaitCard
      key={wait.id}
      wait={wait}
      expanded={expanded.key === wait.id}
      onToggle={() => toggle(wait.id)}
      commandShown={expanded.key === wait.id && expanded.command}
      onToggleCommand={() => setExpanded(current => ({ ...current, command: !current.command }))}
      stop={stops[wait.id]}
      onCancel={onCancel && (() => stopWait(wait.id))}
      onRevealRecovery={onRevealRecovery}
    />
  )
  const summarized = shouldSummarizeHandoffs({ helpers, waits, unlistedHelpers })
  return <>
    <section className={`chat__waits chat__handoffs${summarized && listOpen ? ' chat__handoffs--open' : ''}`} aria-label="Handoffs">
      {resourcePause && (
        <ResourceCard
          resourcePause={resourcePause}
          handoff={handoff}
          onRevealRecovery={onRevealRecovery}
          expanded={expanded.key === 'resource'}
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
              <HelperList helpers={group.helpers} chatId={chatId} onOpenHelper={setOpenHelper} label={`${group.label} helpers`} />
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
          <HelperList helpers={helpers} chatId={chatId} onOpenHelper={setOpenHelper}
            unlisted={unlistedHelpers} automatic={automatic} />
        )}
        {waits.map(waitCard)}
      </>}
    </section>
    {conversationHelper && <HelperEventConversation
      event={conversationHelper}
      chatId={chatId}
      host={openHelper.host}
      onClose={() => setOpenHelper(null)}
      onInternalNav={onInternalNav}
    />}
  </>
}
