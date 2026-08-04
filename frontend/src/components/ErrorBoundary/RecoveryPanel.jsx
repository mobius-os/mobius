import { useId } from 'react'
import { BASE } from '../../api/client.js'
import {
  recoveryPhaseForAttempt,
  repairChatPath,
} from '../../lib/errorRecovery.js'
import RecoveryLink from './RecoveryLink.jsx'
import './RecoveryPanel.css'

function recoveryMessage({ phase, attemptPhase, canAskAgent, hasRepairChat, subject }) {
  const currentSubject = subject === 'screen' ? 'this screen' : 'the app'
  if (phase === 'refresh') {
    return `This ${subject} hit an unexpected error. Refreshing won’t delete your chats.`
  }
  if (phase === 'agent') {
    return `Refreshing didn’t fix ${currentSubject}. Möbius can start a new repair chat and share these technical details with your agent to investigate and fix it.`
  }
  if (!canAskAgent) {
    return `Refreshing didn’t fix ${currentSubject}. Use system recovery to diagnose the problem without relying on this embedded chat.`
  }
  if (attemptPhase === 'agent-directed') {
    // The agent may already be mid-work or waiting on a clarifying question,
    // so waiting is a suggestion, never the only route.
    return hasRepairChat
      ? `The repair request was sent. Give the agent a few minutes, then refresh ${currentSubject} — or open the repair chat to follow along and answer any questions.`
      : `The repair request was sent. Give the agent a few minutes, then refresh ${currentSubject}.`
  }
  // The chat itself may exist — delivery is what failed — so this must not
  // claim otherwise while an "Open repair chat" link sits beside it.
  return 'The repair request didn’t go through. You can retry it, or use system recovery as a last resort.'
}

export default function RecoveryPanel({
  attempt = null,
  canAskAgent = true,
  className = '',
  diagnostic,
  headingRef,
  onAgentRepair,
  onRefresh,
  repairActive = false,
  refreshLabel,
  secondaryAction = null,
  subject,
  title,
  variant,
}) {
  const headingId = useId()
  const phase = recoveryPhaseForAttempt(attempt, { canAskAgent })
  const attemptPhase = attempt?.phase || null
  const repairChatId = attempt?.chatId || null
  const starting = repairActive && attemptPhase === 'agent-starting'
  const agentFailed = attemptPhase === 'agent-failed'
  let primaryAction = null
  if (phase === 'refresh') {
    primaryAction = { label: refreshLabel, onClick: onRefresh }
  } else if (starting) {
    primaryAction = { label: 'Starting repair chat…', onClick: onAgentRepair, disabled: true }
  } else if (phase === 'agent') {
    primaryAction = {
      label: attemptPhase === 'agent-starting' ? 'Resume repair chat' : 'Start repair chat',
      onClick: onAgentRepair,
    }
  } else if (canAskAgent && agentFailed) {
    primaryAction = { label: 'Retry repair chat', onClick: onAgentRepair }
  }

  return (
    <section
      className={`recovery-panel recovery-panel--${variant}${className ? ` ${className}` : ''}`}
      aria-labelledby={headingId}
    >
      <h1
        className="recovery-panel__title"
        id={headingId}
        ref={headingRef}
        tabIndex={-1}
      >
        {title}
      </h1>
      <p className="recovery-panel__body">
        {recoveryMessage({
          phase,
          attemptPhase,
          canAskAgent,
          hasRepairChat: !!repairChatId,
          subject,
        })}
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
        {phase !== 'refresh' && !starting && (
          <button type="button" className="recovery-panel__button" onClick={onRefresh}>
            Refresh again
          </button>
        )}
        {phase === 'recovery' && repairChatId && (
          <a className="recovery-panel__button" href={repairChatPath(repairChatId, BASE)}>
            Open repair chat
          </a>
        )}
        {primaryAction && (
          <button
            type="button"
            className="recovery-panel__button recovery-panel__button--primary"
            onClick={primaryAction.onClick}
            disabled={primaryAction.disabled}
          >
            {primaryAction.label}
          </button>
        )}
      </div>
      {phase === 'recovery' && (
        <RecoveryLink
          className="recovery-panel__recovery"
          lead="If the repair chat can’t get you back in,"
        />
      )}
    </section>
  )
}
