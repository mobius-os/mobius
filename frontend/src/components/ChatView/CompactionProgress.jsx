/* Manual compaction progress stays beside the composer without owning its draft. */
import './CompactionProgress.css'

export default function CompactionProgress({ progress, busy, onContinue, onStartOver, onStop }) {
  if (!progress) return null
  const { state, next_chunk: nextChunk = 0, total_chunks: totalChunks = 0, error } = progress
  const running = state === 'running' || busy
  const stale = state === 'stale'
  const completed = Math.max(0, Math.min(nextChunk, totalChunks))
  return (
    <section className="chat__compaction-progress" aria-label="Compaction progress" aria-live="polite">
      <div className="chat__compaction-progress-copy">
        <strong>{running ? 'Compacting context' : stale ? 'Compaction source changed' : 'Compaction paused'}</strong>
        <span>{totalChunks ? `Completed ${completed} of ${totalChunks} sections.` : 'Preparing sections.'} Your previous session stays in place until this finishes.</span>
        {stale && !running
          ? <span>The chat changed since this draft began. Start over to summarize the current context.</span>
          : <span>Each Continue runs up to eight summarizing requests and may use your provider allowance. It never starts automatically.</span>}
        {error && <span role="alert">{error}</span>}
      </div>
      <div className="chat__compaction-progress-actions">
        {running
          ? <button type="button" onClick={onStop}>Pause compaction</button>
          : <button type="button" disabled={busy} onClick={stale ? onStartOver : onContinue}>
              {stale ? 'Start over' : 'Continue compaction'}
            </button>}
      </div>
    </section>
  )
}
