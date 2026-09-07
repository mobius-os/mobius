/* GoalDraftChip previews the goal a `/goal ` composer draft will start, so the
   goal visual stays open while the owner types the objective. */

/**
 * A compact, non-interactive echo of the active-goal rail, shown while the
 * composer holds a `/goal ` draft. It deliberately does NOT capture keys the
 * way the slash-command menu does: it is a passive status the owner writes
 * "through", so Enter still sends and the objective keeps growing beneath a
 * stable "Goal" label.
 */
export default function GoalDraftChip({ objective }) {
  const hasObjective = !!objective
  return (
    <div
      className="chat__goal-draft"
      role="status"
      aria-live="polite"
      aria-label={hasObjective ? `New goal: ${objective}` : 'Composing a new goal'}
    >
      <span className="chat__goal-draft-tag" aria-hidden="true">Goal</span>
      <span
        className={`chat__goal-draft-text${
          hasObjective ? '' : ' chat__goal-draft-text--hint'
        }`}
      >
        {hasObjective ? objective : 'Describe what to keep working toward…'}
      </span>
    </div>
  )
}
