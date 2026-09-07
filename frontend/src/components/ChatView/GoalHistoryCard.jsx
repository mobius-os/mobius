/* GoalHistoryCard keeps a terminal Goal's outcome beside its final answer. */

import GoalPlanDetails from './GoalPlanDetails.jsx'
import LifecycleIcon, { LifecycleOutcome } from './LifecycleIcon.jsx'
import { goalHistoryViewModel } from './goalHistory.js'

export default function GoalHistoryCard({ summary }) {
  const view = goalHistoryViewModel(summary)
  if (!view) return null

  return (
    <aside
      className={`chat__goal-history chat__goal-history--${view.completed ? 'completed' : 'failed'}`}
      aria-label={view.ariaLabel}
    >
      <LifecycleIcon kind="goal" />
      <div className="chat__goal-history-copy">
        <span className="chat__goal-history-kicker">
          <LifecycleOutcome tone={view.completed ? 'completed' : 'attention'} />{view.kicker}
        </span>
        <strong className="chat__goal-history-objective">{view.objective}</strong>
        {view.metadata && <span className="chat__goal-history-meta">{view.metadata}</span>}
        {view.hasPlan && (
          <details className="chat__goal-history-details">
            <summary>View plan</summary>
            <GoalPlanDetails plan={summary.plan} />
          </details>
        )}
      </div>
    </aside>
  )
}
