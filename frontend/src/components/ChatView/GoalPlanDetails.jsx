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

function delegationMeta(node) {
  const provider = node.provider === 'claude'
    ? 'Claude'
    : node.provider === 'codex'
      ? 'Codex'
      : null
  const state = node.status === 'completed'
    ? 'Complete'
    : node.status === 'paused'
      ? 'Paused'
      : ['starting', 'running', 'resuming'].includes(node.status)
        ? 'In progress'
        : node.status === 'cancelled'
          ? 'Cancelled'
          : node.status === 'stopped'
            ? 'Stopped'
            : 'Needs review'
  return [provider, state].filter(Boolean).join(' · ')
}

function delegationTitle(node) {
  return String(node.task_key || '')
    .replace(/[._-]+/g, ' ')
    .replace(/^./, letter => letter.toUpperCase())
}

export default function GoalPlanDetails({ plan }) {
  const tasks = Array.isArray(plan?.tasks) ? plan.tasks : []
  if (!tasks.length) return null
  const tasksById = new Map(tasks.map(task => [task.id, task]))
  const delegations = Array.isArray(plan?.delegations) ? plan.delegations : []
  const delegatedByTask = new Map(delegations.map(node => [node.task_key, node]))
  const childrenByParent = new Map()
  for (const task of tasks) {
    const parent = tasksById.has(task.parent_id) ? task.parent_id : null
    childrenByParent.set(parent, [...(childrenByParent.get(parent) || []), task])
  }
  const renderBranch = (
    task,
    depth = 0,
    execution = delegatedByTask.get(task.id),
  ) => {
    const planChildren = childrenByParent.get(task.id) || []
    const executionChildren = execution?.children || []
    return <div key={task.id} className="chat__goal-branch" role="none">
      <div
        role="listitem"
        style={{ paddingLeft: `${4 + Math.min(depth, 6) * 18}px` }}
        className={`chat__goal-task chat__goal-task--${task.status}${
          task.ready ? ' chat__goal-task--ready' : ''
        }${task.ready_to_verify ? ' chat__goal-task--verify' : ''}`}
      >
        <span className="chat__goal-task-marker" aria-hidden="true" />
        <span className="chat__goal-task-copy">
          <span className="chat__goal-task-title">{task.title}</span>
          <span className="chat__goal-task-meta">
            {task.ready_to_verify
              ? 'Ready to verify'
              : execution
                ? delegationMeta(execution)
                : taskMeta(task, tasksById)}
          </span>
        </span>
      </div>
      {planChildren.map(child => renderBranch(
        child,
        depth + 1,
        executionChildren.find(node => node.task_key === child.id),
      ))}
      {executionChildren
        .filter(node => !planChildren.some(child => child.id === node.task_key))
        .map(node => renderDelegation(node, depth + 1))}
    </div>
  }
  const renderDelegation = (node, depth = 0) => (
    <div key={node.id} className="chat__goal-branch" role="none">
      <div
        role="listitem"
        style={{ paddingLeft: `${4 + Math.min(depth, 6) * 18}px` }}
        className={`chat__goal-task chat__goal-task--${node.status}`}
      >
        <span className="chat__goal-task-marker" aria-hidden="true" />
        <span className="chat__goal-task-copy">
          <span className="chat__goal-task-title">{delegationTitle(node)}</span>
          <span className="chat__goal-task-meta">{delegationMeta(node)}</span>
        </span>
      </div>
      {(node.children || []).map(child => renderDelegation(child, depth + 1))}
    </div>
  )
  return (
    <div className="chat__goal-plan" role="list" aria-label="Full goal todo list">
      {(childrenByParent.get(null) || []).map(task => renderBranch(task))}
      {delegations
        .filter(node => !tasksById.has(node.task_key))
        .map(node => renderDelegation(node))}
    </div>
  )
}
