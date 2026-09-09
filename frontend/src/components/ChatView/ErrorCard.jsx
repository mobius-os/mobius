import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { formatResetTime } from './resetTime.js'
import LifecycleIcon from './LifecycleIcon.jsx'
import { ChevronRight, Clock, Pause, Warning } from '@openai/apps-sdk-ui/components/Icon'
import MessageCopyButton from './MessageCopyButton.jsx'
import { isResourcePause } from './waitingPresentation.js'

// The single renderer for the error/pause/park card family. MsgContent consumes
// both persisted blocks and the converted live stream, so source selection
// cannot change this card's classification. When the live path had a separate
// renderer, a benign pause flashed danger-red until promotion. Any future field
// that changes how the card reads must land here.
//
// Classification, all from the single `pause` descriptor: a provider-limit
// park carries `pause.resets_at` and reads "Rate limit" — the honest, specific
// name a park deserves. A drain-gated restart carries `pause.kind='restart'`
// without a reset time and reads "Paused". Both are WAIT
// states (any `pause`) and get the soft `.chat__text--parked` treatment; the
// danger-red "Error" card is reserved for genuine failures (no `pause`). Old
// persisted blocks predate `pause` and fall back to the error rendering.
// A platform-resource wait (memory, storage) also carries `resets_at` — its
// re-check time — but it is not a quota: Möbius continues it by itself, so it
// reads as "Waiting" and never offers the auto-continue toggle.
export function errorCardViewModel(block) {
  const resourceWait = isResourcePause(block)
  const parked = !!block.pause?.resets_at && !resourceWait
  // Old saved handoff notes lacked the pause descriptor. Recognize only
  // that exact producer's prefix; unrelated resumable errors remain errors.
  const goalHandoff = block.pause?.kind === 'goal_handoff' || (
    !block.pause && block.resumable === true && block.message?.startsWith(
      'This Goal paused repeatedly without a visible owner for the next action.',
    )
  )
  const benign = !!block.pause || goalHandoff
  return {
    parked,
    resourceWait,
    goalHandoff,
    benign,
    className: `chat__text--error${benign ? ' chat__text--parked' : ''}`,
    label: goalHandoff ? 'Goal paused' : parked ? 'Rate limit' : (resourceWait ? 'Waiting' : (block.pause ? 'Paused' : 'Error')),
    resetLabel: parked ? formatResetTime(block.pause.resets_at) : null,
  }
}

// `children` is the slot for surface-specific affordances — MsgContent
// appends its tail-gated Resume button there; the live surface renders none
// (a terminal error promotes within the same breath, and the button's
// tail-only gate is a persisted-transcript concept).
export default function ErrorCard({
  block,
  autoResume = false,
  resetElapsed = false,
  cardRef,
  children,
}) {
  const vm = errorCardViewModel(block)
  const recoveryTitle = vm.parked
    ? autoResume
      ? (vm.resetLabel ? `Queued to continue ${vm.resetLabel}` : 'Queued to continue')
      : resetElapsed
        ? 'Usage is available again'
        : (vm.resetLabel ? `Usage resets ${vm.resetLabel}` : 'Usage limit reached')
    : null
  const recoveryCopy = vm.parked
    ? autoResume
      ? 'Your work is safe. Möbius will continue automatically at the reset. Added credits or reset usage? You can try now.'
      : resetElapsed
        ? 'Your work is safe. Continue when you’re ready.'
        : 'Your work is safe. Turn on auto-continue, or try now after adding credits or resetting usage.'
    : null
  return (
    <div className={vm.className} ref={cardRef}>
      <LifecycleIcon>{vm.parked || vm.resourceWait
        ? <Clock width={18} height={18} />
        : vm.benign ? <Pause width={18} height={18} />
          : <Warning width={18} height={18} />}</LifecycleIcon>
      {/* Keep the announced status body separate from interactive children.
          Otherwise a switch update or nested save alert makes the atomic
          status region re-announce the whole rate-limit card. */}
      <div
        className="chat__error-status"
        role={vm.benign ? undefined : 'alert'}
      >
        {vm.parked ? (
          <>
            <div className="chat__recovery-title">{recoveryTitle}</div>
            <div className="chat__recovery-copy">{recoveryCopy}</div>
            {block.message && (
              <details className="chat__recovery-details">
                <summary>
                  <ChevronRight
                    className="chat__recovery-details-chevron"
                    width={14}
                    height={14}
                    aria-hidden="true"
                  />
                  Technical details
                </summary>
                {/* Provider payloads sometimes carry useful quota links. Keep
                    them available without making internal codes the headline. */}
                <StandardMarkdown text={block.message} />
              </details>
            )}
          </>
        ) : vm.benign ? (
          <>
            <div className="chat__recovery-title chat__recovery-title--paused">
              {vm.label}
            </div>
            <div className="chat__recovery-copy">
              {vm.goalHandoff
                ? 'The agent stopped before arranging the next step. Your progress is saved. Resume to continue this Goal.'
                : block.pause?.kind === 'restart'
                ? block.resumable
                  ? 'Möbius will continue automatically when the restart is complete.'
                  : (block.message || 'This response is paused.')
                : vm.resourceWait
                  ? (block.message || 'Möbius will continue automatically when resources free up.')
                  : (block.message || 'Möbius will continue automatically.')}
            </div>
          </>
        ) : (
          <>
            {/* Header row: the "Error" label plus a one-tap copy of the raw
                message. Copying the full text from a button sidesteps native
                long-press selection, which is unreliable on phones and drops
                content once the message scrolls partly off-screen. */}
            <div className="chat__error-head">
              <span className="chat__error-label">{vm.label}</span>
              {block.message && <MessageCopyButton text={block.message} />}
            </div>
            <StandardMarkdown
              text={block.message || 'The agent ran into an issue.'}
            />
          </>
        )}
      </div>
      {children}
    </div>
  )
}
