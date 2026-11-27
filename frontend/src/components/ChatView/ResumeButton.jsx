// The one Resume control for every card that offers to continue paused work.
// The caller names the idle and pending words and what a press does; the
// pending, reconnecting and switch-in-progress states are the same everywhere.
export default function ResumeButton({
  label,
  pendingLabel,
  onClick,
  resumeState,
  submissionBlocked = false,
}) {
  return (
    <button
      type="button"
      className="chat__resume chat__recovery-action"
      onClick={onClick}
      disabled={submissionBlocked || resumeState?.pending || resumeState?.unavailable}
      aria-busy={resumeState?.pending || undefined}
      title={submissionBlocked
        ? 'Wait for the provider switch to finish.'
        : resumeState?.unavailable
          ? 'Möbius is reconnecting this paused turn.'
        : undefined}
    >
      {resumeState?.pending
        ? pendingLabel
        : resumeState?.unavailable ? 'Reconnecting…'
        : label}
    </button>
  )
}
