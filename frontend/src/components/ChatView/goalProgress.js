import { goalHelperWaitingLabel, goalHelpers } from './goalHelpers.js'

/* Message formatting and the server-owned Goal progress view model. */

/**
 * Return the objective carried by a real leading `/goal` command.
 *
 * The backend intentionally recognizes commands only at character zero (with
 * leading newlines tolerated), so the indicator follows that same boundary
 * instead of lighting up for ordinary prose that happens to mention `/goal`.
 * Whitespace is collapsed because the footer is a one-line status surface.
 */
function goalCommandObjective(text) {
  if (typeof text !== 'string') return ''
  const normalized = text.replace(/^\n+/, '')
  const match = normalized.match(/^\/goal(?:\s+([\s\S]*))?$/)
  if (!match) return ''
  const objective = (match[1] || '').trim()
  const compactObjective = objective.replace(/\s+/g, ' ')
  if (!compactObjective || compactObjective.toLowerCase() === 'clear') return ''
  return objective
}

/** Canonical one-line objective used by every compact Goal surface. */
export function compactGoalObjective(objective) {
  return String(objective || '').replace(/\s+/g, ' ').trim()
}

/** Keep the owner's formatting while hiding the command token in the bubble. */
export function goalMessageObjectiveFromText(text) {
  return goalCommandObjective(text)
}

/**
 * The objective a `/goal ` composer draft is building, or null when the draft
 * is not a goal command.
 *
 * The draft chip is meant to take over exactly as the slash-command menu
 * dismisses, so it requires the whitespace after `/goal` that closes that menu
 * (see slashQueryFor) — a bare `/goal` still belongs to the picker. It returns
 * '' (not null) once that space exists but before an objective is typed, so the
 * composer can show the goal is armed while the owner is still writing it.
 * `/goal clear` is a control phrase, never a new objective, so it shows no chip.
 */
export function draftGoalObjective(text) {
  if (typeof text !== 'string') return null
  const normalized = text.replace(/^\n+/, '')
  const match = normalized.match(/^\/goal\s([\s\S]*)$/)
  if (!match) return null
  const objective = compactGoalObjective(match[1])
  if (objective.toLowerCase() === 'clear') return null
  return objective
}

const GOAL_PRESENTATION_STATUSES = new Set([
  'active', 'paused', 'completed', 'cannot_complete', 'cancelled',
])

/** Normalize the durable Goal presentation shared by detail/runtime reads. */
export function normalizeGoalPresentation(goal) {
  if (!goal || typeof goal !== 'object') return null
  const objective = compactGoalObjective(goal.objective)
  if (!objective || !GOAL_PRESENTATION_STATUSES.has(goal.status)) return null
  return {
    id: goal.id == null ? null : String(goal.id),
    ...(Number.isInteger(goal.revision) ? { revision: goal.revision } : {}),
    objective,
    status: goal.status,
    resumable: goal.resumable === true,
    plan: goal.plan || null,
    ...(goal.status === 'paused' && ['owner', 'agent', 'unknown', 'deferred'].includes(goal.pause_reason)
      ? { pause_reason: goal.pause_reason } : {}),
    ...(goal.status === 'paused' && goal.pause_reason === 'deferred' && typeof goal.hold_reason === 'string'
      ? { hold_reason: goal.hold_reason } : {}),
    ...(goal.handoff?.kind ? { handoff: goal.handoff } : {}),
    ...(goal.result ? { result: goal.result } : {}),
  }
}

/** One status vocabulary for the Goal rail and its accessible announcement.
 * Only the exact Goal's handoff may describe who moves next; chat cards cannot.
 */
export function goalStatusLabel(goal) {
  if (!goal) return null
  const terminal = {
    completed: 'Completed', cannot_complete: 'Cannot complete', cancelled: 'Cancelled',
  }[goal.status]
  if (terminal) return terminal
  if (goal.status === 'paused') {
    const paused = {
      owner: 'Paused by you', agent: 'Paused by agent', unknown: 'Interrupted', deferred: 'On hold',
    }[goal.pause_reason]
    if (paused) return paused
  }
  if (goal.handoff?.kind === 'owner_input') return 'Waiting for you'
  if (goal.handoff?.kind === 'automatic') return 'Waiting'
  return goal.status === 'paused' ? 'Interrupted' : null
}

/** Required recovery is available only when the Goal permits it and no executor owns the next move. */
export function canResumeGoal(goal, { turnActive, hasPendingQuestion, chatHandoff } = {}) {
  return goal?.status === 'paused'
    && goal.resumable === true
    && !turnActive
    && !hasPendingQuestion
    && !['automatic', 'owner_input'].includes(goal.handoff?.kind)
    && !['automatic', 'owner_input'].includes(chatHandoff)
}

/** Runtime/detail is the sole lifecycle owner; message text is formatting only. */
export function goalPresentationFromRuntime(runtime) {
  return normalizeGoalPresentation(runtime?.goal)
}

/**
 * Put the retained Goal and ordinary build phases on one existing progress rail.
 *
 * The last item is current: before a build phase arrives that is the Goal
 * itself; afterwards the Goal remains as quiet context while the newest phase
 * carries emphasis. Settled Goal status belongs to this same item rather than
 * a second completion banner.
 */
function progressLabel(task) {
  const progress = task?.progress
  if (Number.isInteger(progress?.current) && Number.isInteger(progress?.total)) {
    return `${task.title} · ${progress.current}/${progress.total}`
  }
  return task?.title || ''
}

/**
 * A task with a helper still working shows as running, whatever it was marked;
 * otherwise the task's own status stands. A failed helper remains in execution history, since another helper may
 * already have redone its work.
 */
export function goalTaskDisplayStatus(task, helpers = []) {
  const active = ['accepted', 'retrying', 'starting', 'running', 'resuming', 'paused', 'needs_input']
  return helpers.some(helper => active.includes(helper?.status))
    ? 'running'
    : task?.status
}

function deepestPlanTasks(tasks, candidates) {
  const byId = new Map(tasks.map(task => [task.id, task]))
  const candidateIds = new Set(candidates.map(task => task.id))
  const shadowedAncestors = new Set()
  for (const task of candidates) {
    let parent = byId.get(task.parent_id)
    const visited = new Set()
    while (parent && !visited.has(parent.id)) {
      visited.add(parent.id)
      if (candidateIds.has(parent.id)) shadowedAncestors.add(parent.id)
      parent = byId.get(parent.parent_id)
    }
  }
  return candidates.filter(task => !shadowedAncestors.has(task.id))
}

/** Active work first; when nothing is running, expose every newly ready task. */
export function visibleGoalTasks(goalPlan) {
  const activeStatuses = new Set(['accepted', 'retrying', 'starting', 'running', 'resuming', 'paused', 'needs_input'])
  const tasks = Array.isArray(goalPlan?.tasks) ? goalPlan.tasks : []
  const delegatedTasks = new Set(goalHelpers({ plan: goalPlan })
    .filter(node => activeStatuses.has(node.status))
    .map(node => node.plan_task))
  const working = tasks.filter(task => delegatedTasks.has(task.id) || task.status === 'running')
  if (working.length) return deepestPlanTasks(tasks, working)
  return deepestPlanTasks(tasks, tasks.filter(task => task?.ready === true))
}

export function progressRailViewModel(
  goal,
  buildPhases,
  { turnActive = false } = {},
) {
  const items = []
  const presentation = normalizeGoalPresentation(goal)
  const goalPlan = presentation?.plan
  const goalObjective = presentation?.objective || ''
  if (goalObjective) {
    const completed = goalPlan?.summary?.completed
    const total = goalPlan?.summary?.total
    const planned = Number.isInteger(completed) && Number.isInteger(total)
    const activeTasks = visibleGoalTasks(goalPlan)
    const activeLabels = activeTasks.map(progressLabel).filter(Boolean)
    const statusLabel = goalHelperWaitingLabel(presentation, { turnActive }) || goalStatusLabel(presentation)
    const progressSummary = planned ? `${completed}/${total}` : goalObjective
    items.push({
      key: 'goal',
      label: `Goal${statusLabel ? ` · ${statusLabel}` : ''} · ${progressSummary}${
        activeLabels.length
          ? ` · ${activeLabels.join(' + ')}`
          : ''
      }`,
      expandable: true,
      tone: presentation.status,
      ...(goalPlan ? {
        title: `Goal: ${goalObjective}`,
        ariaLabel: `Goal: ${goalObjective}. ${statusLabel || 'Working'}; ${completed} of ${total} complete`,
      } : {}),
    })
  }
  const phases = Array.isArray(buildPhases) ? buildPhases : []
  for (const phase of phases) {
    if (!phase?.label) continue
    items.push({
      key: `phase-${phase.ts}`,
      label: phase.label,
    })
  }
  return items.map((item, index) => ({
    ...item,
    current: index === items.length - 1,
  }))
}
