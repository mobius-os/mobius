/* WaitingChip renders a chat's armed durable waits: the visible form of "the
   agent is waiting for X and will resume on its own". One chip per wait, shown
   only while no turn is active — during a live turn the run surface already
   owns the status area. Cancel is immediate and local-first; the durable row
   is cancelled through the platform. */

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

export default function WaitingChip({ waits, onCancel }) {
  if (!waits?.length) return null
  return (
    <div className="chat__waits" role="status" aria-live="polite">
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
