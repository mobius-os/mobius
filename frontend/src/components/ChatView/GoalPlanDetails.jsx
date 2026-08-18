/* GoalPlanDetails renders the expanded dependency-aware todo list. */

function taskMeta(task, tasksById) {
  if (task.status === 'running') {
    const progress = task.progress
    return Number.isInteger(progress?.current) && Number.isInteger(progress?.total)
      ? `${progress.current} of ${progress.total}`
      : 'In progress'
  }
  if (task.status === 'completed') return 'Complete'
  if (task.status === 'blocked') return task.note || 'Blocked'
  if (task.status === 'failed') return task.note || 'Failed'
  if (task.status === 'cancelled') return 'Cancelled'
  if (task.ready) return 'Ready'
  const waiting = (task.waiting_on || [])
    .map(id => tasksById.get(id)?.title || id)
  return waiting.length ? `Waiting for ${waiting.join(' + ')}` : 'Pending'
}

export default function GoalPlanDetails({ plan }) {
  const tasks = Array.isArray(plan?.tasks) ? plan.tasks : []
  if (!tasks.length) return null
  const tasksById = new Map(tasks.map(task => [task.id, task]))
  return (
    <div className="chat__goal-plan" role="list" aria-label="Full goal todo list">
      {tasks.map(task => (
        <div
          key={task.id}
          role="listitem"
          className={`chat__goal-task chat__goal-task--${task.status}${
            task.ready ? ' chat__goal-task--ready' : ''
          }`}
        >
          <span className="chat__goal-task-marker" aria-hidden="true" />
          <span className="chat__goal-task-copy">
            <span className="chat__goal-task-title">{task.title}</span>
            <span className="chat__goal-task-meta">
              {taskMeta(task, tasksById)}
            </span>
          </span>
        </div>
      ))}
    </div>
  )
}
