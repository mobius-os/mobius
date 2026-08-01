import { useId } from 'react'
import { BASE } from '../../api/client.js'
import {
  recoveryActionPolicy,
  repairChatPath,
} from '../../lib/errorRecovery.js'
import RecoveryLink from './RecoveryLink.jsx'
import './RecoveryPanel.css'

function recoveryMessage({ phase, attemptPhase, canAskAgent, subject }) {
  const currentSubject = subject === 'screen' ? 'this screen' : 'the app'
  if (phase === 'refresh') {
    return `This ${subject} hit an unexpected error. Refreshing won’t delete your chats.`
  }
  if (phase === 'agent' || phase === 'agent-starting') {
    return `Refreshing didn’t fix ${currentSubject}. Möbius can start a new repair chat and share these technical details with your agent to investigate and fix it.`
  }
  if (!canAskAgent) {
    return `Refreshing didn’t fix ${currentSubject}. Use system recovery to diagnose the problem without relying on this embedded chat.`
  }
  if (attemptPhase === 'agent-directed') {
    return `The repair chat started, but ${currentSubject} still can’t open. System recovery is the remaining fallback.`
  }
  return 'The repair chat couldn’t start. You can retry it or use system recovery as a last resort.'
}

export default function RecoveryPanel({
  canAskAgent = true,
  className = '',
  diagnostic,
  headingId,
  headingRef,
  onAgentRepair,
  onRefresh,
  phase,
  refreshLabel,
  repairChatId,
  secondaryAction = null,
  subject,
  title,
  attemptPhase,
  variant,
}) {
  const generatedHeadingId = useId()
  const resolvedHeadingId = headingId || generatedHeadingId
  const actions = recoveryActionPolicy({
    phase,
    attemptPhase,
    canAskAgent,
    repairChatId,
  })
  const starting = phase === 'agent-starting'
  const agentFailed = attemptPhase === 'agent-failed'
  const showPrimary = phase === 'refresh'
    || actions.showAskAgent
    || actions.showRetryAgent
    || starting
  const primaryLabel = phase === 'refresh'
    ? refreshLabel
    : starting
      ? 'Starting repair chat…'
      : phase === 'agent' && attemptPhase === 'agent-starting'
        ? 'Resume repair chat'
        : actions.showRetryAgent
          ? 'Retry repair chat'
          : 'Start repair chat'

  return (
    <section
      className={`recovery-panel recovery-panel--${variant}${className ? ` ${className}` : ''}`}
      aria-labelledby={resolvedHeadingId}
    >
      <h1
        className="recovery-panel__title"
        id={resolvedHeadingId}
        ref={headingRef}
        tabIndex={-1}
      >
        {title}
      </h1>
      <p className="recovery-panel__body">
        {recoveryMessage({ phase, attemptPhase, canAskAgent, subject })}
      </p>
      <details className="recovery-panel__details">
        <summary>Technical details</summary>
        <pre className="recovery-panel__detail">{diagnostic}</pre>
      </details>
      {(starting || agentFailed) && (
        <p
          className={`recovery-panel__status${agentFailed ? ' recovery-panel__status--sr-only' : ''}`}
          role="status"
          aria-live="polite"
        >
          {starting
            ? 'Starting the repair chat. This may take a moment.'
            : 'Repair chat request failed.'}
        </p>
      )}
      <div
        className="recovery-panel__actions"
        aria-busy={starting ? true : undefined}
      >
        {secondaryAction && (
          <a className="recovery-panel__button" href={secondaryAction.href}>
            {secondaryAction.label}
          </a>
        )}
        {actions.showRefreshAgain && (
          <button type="button" className="recovery-panel__button" onClick={onRefresh}>
            Refresh again
          </button>
        )}
        {actions.showOpenRepairChat && (
          <a className="recovery-panel__button" href={repairChatPath(repairChatId, BASE)}>
            Open repair chat
          </a>
        )}
        {showPrimary && (
          <button
            type="button"
            className="recovery-panel__button recovery-panel__button--primary"
            onClick={phase === 'refresh' ? onRefresh : onAgentRepair}
            disabled={starting}
          >
            {primaryLabel}
          </button>
        )}
      </div>
      {actions.showRecovery && (
        <RecoveryLink
          className="recovery-panel__recovery"
          lead="If the repair chat can’t get you back in,"
        />
      )}
    </section>
  )
}
