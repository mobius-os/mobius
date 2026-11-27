import ResumeButton from './ResumeButton.jsx'

// The actions of a paused turn's card, in the order a person weighs them:
// retry now, or move to another provider, side by side; then the quiet
// auto-continue choice under them. Each part appears only when it applies, so
// a card with just a retry (a restart, a storage wait) renders that alone.
export default function RecoveryActions({
  resume,
  onSwitchProvider,
  autoResume,
  resumeState,
  submissionBlocked,
}) {
  const primary = resume || onSwitchProvider
  if (!primary && !autoResume) return null
  return (
    <div className="chat__recovery-actionbar">
      {primary && (
        <div className="chat__recovery-actions chat__recovery-actions--pair">
          {resume && (
            <ResumeButton
              label={resume.label}
              pendingLabel={resume.pendingLabel}
              onClick={resume.onClick}
              resumeState={resumeState}
              submissionBlocked={submissionBlocked}
            />
          )}
          {onSwitchProvider && (
            <button
              type="button"
              className="chat__recovery-action chat__recovery-action--outline"
              onClick={onSwitchProvider}
              disabled={submissionBlocked}
            >
              Switch provider
            </button>
          )}
        </div>
      )}
      {autoResume && (
        <button
          type="button"
          className="chat__recovery-action chat__recovery-action--text"
          onClick={autoResume.onToggle}
          disabled={autoResume.saving || submissionBlocked}
        >
          {autoResume.saving
            ? 'Saving…'
            : autoResume.enabled
              ? 'Turn off auto-continue'
              : 'Auto-continue this chat'}
        </button>
      )}
      {autoResume?.error && (
        <span className="chat__recovery-action-error" role="alert">
          {autoResume.error}
        </span>
      )}
    </div>
  )
}
