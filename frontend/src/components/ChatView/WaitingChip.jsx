/* WaitingChip renders every self-resuming chat handoff: declared monitors and
   wake-enabled background helpers. It is shown only while no parent turn is
   active; during a live turn the run surface already owns the status area. */

function intervalLabel(wait) {
  if (wait.kind === 'timer') {
    return wait.due_at
      ? `resumes ${new Date(wait.due_at + 'Z').toLocaleTimeString([], {
          hour: '2-digit', minute: '2-digit',
        })}`
      : 'resumes later'
  }
  const secs = Number(wait.interval_secs) || 300
  const minutes = Math.round(secs / 60)
  return minutes <= 1 ? 'checking every minute' : `checking every ${minutes} min`
}

function helperTaskLabel(taskKey) {
  return String(taskKey || '')
    .replace(/[._-]+/g, ' ')
    .replace(/^./, letter => letter.toUpperCase())
}

export default function WaitingChip({ waits = [], backgroundHelpers, onCancel }) {
  const helperCount = Number(backgroundHelpers?.count) || 0
  if (!waits.length && helperCount === 0) return null
  const helperTasks = (backgroundHelpers?.items || [])
    .map(item => helperTaskLabel(item?.task_key))
    .filter(Boolean)
  return (
    <div className="chat__waits" role="status" aria-live="polite">
      {helperCount > 0 && (
        <div className="chat__wait-chip">
          <span className="chat__wait-tag" aria-hidden="true">
            <span className="chat__wait-pulse" />
            Waiting
          </span>
          <span
            className="chat__wait-text"
            title={helperTasks.length ? helperTasks.join(', ') : undefined}
          >
            Waiting on {helperCount} {helperCount === 1 ? 'helper' : 'helpers'}
          </span>
          <span className="chat__wait-meta">resumes automatically</span>
        </div>
      )}
      {waits.map(wait => (
        <div key={wait.id} className="chat__wait-chip">
          <span className="chat__wait-tag" aria-hidden="true">
            <span className="chat__wait-pulse" />
            Waiting
          </span>
          <span
            className="chat__wait-text"
            title={`${wait.description} — ${intervalLabel(wait)}`}
          >
            {wait.description}
          </span>
          <span className="chat__wait-meta">{intervalLabel(wait)}</span>
          <button
            type="button"
            className="chat__wait-cancel"
            aria-label={`Stop waiting for: ${wait.description}`}
            onClick={() => onCancel?.(wait.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
