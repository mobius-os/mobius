import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { formatResetTime, isProviderLimitPause, pauseTiming } from './resetTime.js'
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
// park carries `pause.kind='usage_limit'` or `rate_limit`. Its check_at is a
// bounded retry clock, not proof of a provider reset. A restart pause carries
// `pause.kind='restart'`
// without a reset time and reads "Paused". Both are WAIT
// states (any `pause`) and get the soft `.chat__text--parked` treatment; the
// danger-red "Error" card is reserved for genuine failures (no `pause`). Old
// persisted blocks predate `pause` and fall back to the error rendering.
// A platform-resource wait (memory, storage) carries a check time but is not a
// quota: Möbius continues it by itself, so it
// reads as "Waiting" and never offers the auto-continue toggle.
export function errorCardViewModel(block) {
  const credits = block.pause?.kind === 'credits'
  const resourceWait = isResourcePause(block)
  const modelCapacity = block.pause?.kind === 'model_capacity'
  const modelCapacityExhausted = block.pause?.kind === 'model_capacity_exhausted'
  const { checkAt, resetAt } = pauseTiming(block.pause)
  const parked = isProviderLimitPause(block.pause) || (!!checkAt && !block.pause?.kind)
  // Old saved handoff notes lacked the pause descriptor. Recognize only
  // that exact producer's prefix; unrelated resumable errors remain errors.
  const goalHandoff = block.pause?.kind === 'goal_handoff' || (
    !block.pause && block.resumable === true && block.message?.startsWith(
      'This Goal paused repeatedly without a visible owner for the next action.',
    )
  )
  const benign = !!block.pause || goalHandoff
  return {
    credits,
    parked,
    modelCapacity,
    modelCapacityExhausted,
    resourceWait,
    goalHandoff,
    benign,
    className: `chat__text--error${benign ? ' chat__text--parked' : ''}`,
    label: credits ? 'Credits needed' : goalHandoff ? 'Goal paused' : modelCapacityExhausted ? 'Model still busy' : modelCapacity ? 'Model busy' : parked ? 'Rate limit' : (resourceWait ? 'Waiting' : (block.pause ? 'Paused' : 'Error')),
    checkLabel: formatResetTime(checkAt),
    resetLabel: parked ? formatResetTime(resetAt) : null,
  }
}

// `children` is the slot for surface-specific affordances — MsgContent
// appends its tail-gated Resume button there; the live surface renders none
// (a terminal error promotes within the same breath, and the button's
// tail-only gate is a persisted-transcript concept).
export default function ErrorCard({
  block,
  autoResume = false,
  continuationWait = null,
  manualRecovery = false,
  resetElapsed = false,
  recoveryCredit = null,
  cardRef,
  children,
}) {
  const vm = errorCardViewModel(block)
  // A retry deadline cannot promise progress while platform recovery holds
  // automatic admission. This live status never rewrites the original error.
  const platformHold = !block.pause?.manual && (vm.modelCapacity || vm.parked || vm.resourceWait || block.pause?.kind === 'restart') && (
    continuationWait === 'restart_required' || continuationWait === 'restoring_edits'
  )
  const recoveryTitle = platformHold
    ? (continuationWait === 'restart_required' ? 'Waiting for a server restart' : 'Waiting for the platform update')
    : vm.modelCapacity
    ? !manualRecovery
      ? (vm.checkLabel ? `Trying again ${vm.checkLabel}` : 'Trying again shortly')
      : 'Model recovery needs attention'
    : vm.parked
    ? autoResume
      ? (vm.checkLabel ? `Queued to retry ${vm.checkLabel}` : 'Queued to retry')
      : resetElapsed
        ? 'Ready to retry'
        : 'Provider limit reached'
    : null
  const recoveryCopy = platformHold
    ? block.pause?.kind === 'restart'
      ? continuationWait === 'restart_required'
        ? 'Waiting for a server restart to load the restored work. This chat will continue after those changes are loaded.'
        : 'Waiting for the update to restore unfinished work. This chat will continue once that work is restored and loaded.'
      : (continuationWait === 'restart_required'
      ? 'Your work is saved. Automatic retries are paused until a server restart loads the restored work. Möbius will retry after those changes are loaded.'
      : 'Your work is saved. Automatic retries are paused while the update restores unfinished work. Möbius will retry once that work is restored and loaded.')
    : vm.modelCapacity
    ? !manualRecovery
      ? 'Your work is safe. Möbius will retry with increasing pauses, up to five times. If the model stays busy, you can choose another model and Resume.'
      : 'Automatic model recovery is unavailable. Choose another model and Resume your saved work.'
    : vm.parked
    ? autoResume
      ? `Your work is safe. ${recoveryCredit?.label ? `${recoveryCredit.label}. ` : ''}Möbius will check again${vm.checkLabel ? ` ${vm.checkLabel}` : ' automatically'}; the provider may still be limited.`
      : resetElapsed
        ? 'Your work is safe. You can retry now; the provider may still be limited.'
        : recoveryCredit?.label
          ? `Your work is safe. ${recoveryCredit.label}. Continuing now may use it.`
          : 'Your work is safe. Turn on auto-continue, or try again manually.'
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
        {vm.parked || vm.modelCapacity ? (
          <>
            <div className="chat__recovery-title">{recoveryTitle}</div>
            <div className="chat__recovery-copy">{recoveryCopy}</div>
            {vm.parked && <div className="chat__recovery-copy">
              {vm.resetLabel ? `Provider reports the limit resets ${vm.resetLabel}.` : 'Provider reset time unknown.'}
              {vm.checkLabel ? ` Next retry check ${vm.checkLabel}.` : ''}
            </div>}
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
              {platformHold ? recoveryTitle : vm.resourceWait && manualRecovery ? 'Recovery needed' : vm.label}
            </div>
            <div className="chat__recovery-copy">
              {platformHold ? recoveryCopy : vm.credits
                ? 'Your workspace is out of credits. Your progress is saved. Add credits to your workspace or choose another provider, then Resume.'
                : vm.modelCapacityExhausted
                ? 'Five automatic retries were used. Choose another model, then Resume to continue your saved work.'
                : vm.goalHandoff
                ? 'The agent stopped before arranging the next step. Your progress is saved. Resume to continue this Goal.'
                : block.pause?.kind === 'restart'
                ? block.resumable
                  ? block.pause.manual
                    ? 'Your work is saved. Resume to continue.'
                    : manualRecovery
                      ? 'This restart needs manual recovery. Your work is saved; Resume to continue.'
                    : 'Möbius will continue automatically when the restart is complete.'
                  : (block.message || 'This response is paused.')
                : vm.resourceWait
                  ? !manualRecovery
                    ? (block.message || 'Möbius will continue automatically when resources free up.')
                    : 'Automatic resource recovery is unavailable. Your work is saved; Resume to continue.'
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
