/* GoalPlanDetails renders the expanded dependency-aware todo list. */

import { Check } from '@openai/apps-sdk-ui/components/Icon'
import { goalTaskDisplayStatus } from './goalProgress'
import { goalHelpers } from './goalHelpers'

function taskMeta(task, tasksById) {
  if (task.status === 'running') {
    const progress = task.progress
    return Number.isInteger(progress?.current) && Number.isInteger(progress?.total)
      ? `${progress.current} of ${progress.total}`
      : 'In progress'
  }
  if (task.status === 'completed') return 'Complete'
  if (task.status === 'blocked') return task.note ? `Blocked · ${task.note}` : 'Blocked'
  if (task.status === 'failed') return task.note ? `Failed · ${task.note}` : 'Failed'
  if (task.status === 'cancelled') return 'Cancelled'
  if (task.ready) return 'Ready'
  const waiting = (task.waiting_on || [])
    .map(id => tasksById.get(id)?.title || id)
  return waiting.length ? `Waiting for ${waiting.join(' + ')}` : 'Pending'
}

function delegationMeta(node) {
  const state = node.status === 'completed'
    ? 'Complete'
    : node.status === 'paused'
      ? 'On hold'
      : ['accepted', 'retrying', 'starting', 'running', 'resuming'].includes(node.status)
        ? 'In progress'
        : node.status === 'cancelled'
          ? 'Cancelled'
          : node.status === 'stopped'
            ? 'Stopped'
          : node.status === 'needs_input'
            ? 'Needs an answer'
            : 'Needs review'
  return state
}

function GoalPlanRow({ title, status, meta, depth, emphasized, children }) {
  const hasChildren = Array.isArray(children) ? children.length > 0 : !!children
  return (
    <div className="chat__goal-branch" role="listitem">
      <div
        style={{ paddingLeft: `${4 + Math.min(depth, 6) * 18}px` }}
        className={`chat__goal-task chat__goal-task--${status}${
          emphasized ? ` chat__goal-task--${emphasized}` : ''
        }`}
      >
        <span className="chat__goal-task-marker" aria-hidden="true">
          {status === 'completed' && <Check width={12} height={12} />}
        </span>
        <span className="chat__goal-task-copy">
          <span className="chat__goal-task-title">{title}</span>
          <span className="chat__goal-task-meta">{meta}</span>
        </span>
      </div>
      {hasChildren && <div className="chat__goal-children" role="list">{children}</div>}
    </div>
  )
}

export default function GoalPlanDetails({ plan, holdReason = null }) {
  const reason = typeof holdReason === 'string' ? holdReason.trim() : ''
  const tasks = Array.isArray(plan?.tasks) ? plan.tasks : []
  if (!tasks.length && !plan?.delegations?.length && !reason) return null
  const tasksById = new Map(tasks.map(task => [task.id, task]))
  const delegations = Array.isArray(plan?.delegations) ? plan.delegations : []
  // Execution is evidence for task status, never another checklist step.
  const helpersByTask = new Map()
  const allHelpers = goalHelpers({ plan })
  for (const node of allHelpers) {
    helpersByTask.set(node.plan_task, [...(helpersByTask.get(node.plan_task) || []), node])
  }
  const childrenByParent = new Map()
  for (const task of tasks) {
    const parent = tasksById.has(task.parent_id) ? task.parent_id : null
    childrenByParent.set(parent, [...(childrenByParent.get(parent) || []), task])
  }
  const renderBranch = (task, depth = 0) => {
    const helpers = helpersByTask.get(task.id) || []
    const children = [
      ...(childrenByParent.get(task.id) || []).map(child => renderBranch(child, depth + 1)),
    ]
    return <GoalPlanRow
      key={task.id}
      title={task.title}
      status={goalTaskDisplayStatus(task, helpers)}
      depth={depth}
      emphasized={task.ready_to_verify ? 'verify' : task.ready ? 'ready' : ''}
      meta={goalTaskDisplayStatus(task, helpers) !== task.status
        ? 'In progress' : task.ready_to_verify ? 'Ready to verify' : taskMeta(task, tasksById)}
    >
      {children}
    </GoalPlanRow>
  }
  return (
    <div className="chat__goal-plan" role="region" aria-label="Goal details" tabIndex={0}>
      {reason && <p className="chat__goal-result">{reason}</p>}
      <div className="chat__goal-plan-tasks" role="list" aria-label="Full goal todo list">
        {(childrenByParent.get(null) || []).map(task => renderBranch(task))}
      </div>
      {!!delegations.length && <details className="chat__goal-execution">
        <summary>Helper activity · {allHelpers.length}</summary>
        <p className="chat__goal-task-meta">Execution history, not additional checklist steps.</p>
        <ul>
          {allHelpers.map(node => <li key={node.id}>
            <span>{node.title || tasksById.get(node.plan_task)?.title || 'Unassigned helper'}</span>
            <span className="chat__goal-task-meta">{delegationMeta(node)}</span>
          </li>)}
        </ul>
      </details>}
    </div>
  )
}
