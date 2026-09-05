/* WaitHistoryCard preserves a settled monitor outcome beside chat history. */

import { waitHistoryViewModel } from './waitHistory.js'

export default function WaitHistoryCard({ summary }) {
  const view = waitHistoryViewModel(summary)
  if (!view) return null

  return (
    <aside
      className={`chat__goal-history chat__wait-history chat__wait-history--${view.tone}`}
      aria-label={view.ariaLabel}
    >
      <span
        className="chat__goal-history-marker chat__wait-history-marker"
        aria-hidden="true"
      />
      <div className="chat__goal-history-copy">
        <span className="chat__goal-history-kicker chat__wait-history-kicker">
          {view.kicker}
        </span>
        <strong className="chat__goal-history-objective">{view.condition}</strong>
        {view.metadata && (
          <span className="chat__goal-history-meta">{view.metadata}</span>
        )}
      </div>
    </aside>
  )
}
