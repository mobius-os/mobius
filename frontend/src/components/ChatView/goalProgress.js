/* Goal-command parsing and the shared footer progress-rail view model. */

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

export function goalObjectiveFromText(text) {
  return compactGoalObjective(goalCommandObjective(text))
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

/** Keep a live event from being regressed by an older initial fetch. */
export function newestGoalPlan(current, incoming) {
  if (!incoming) return current || null
  if (!current) return incoming
  if (
    current.root_run_id === incoming.root_run_id
    && Number.isInteger(current.revision)
    && Number.isInteger(incoming.revision)
    && current.revision > incoming.revision
  ) {
    return current
  }
  return incoming
}

function isContinue(text) {
  return typeof text === 'string' && text.trim().toLowerCase() === 'continue'
}

const GOAL_PRESENTATION_STATUSES = new Set([
  'active', 'paused', 'completed', 'failed',
])
const GOAL_WAIT_KINDS = new Set(['owner_question', 'monitor'])

/** Normalize the durable Goal presentation shared by detail/runtime reads. */
export function normalizeGoalPresentation(goal) {
  if (!goal || typeof goal !== 'object') return null
  const objective = compactGoalObjective(goal.objective)
  if (!objective || !GOAL_PRESENTATION_STATUSES.has(goal.status)) return null
  const waitKind = GOAL_WAIT_KINDS.has(goal.wait_kind) ? goal.wait_kind : null
  return {
    id: goal.id == null ? null : String(goal.id),
    objective,
    status: goal.status,
    resumable: goal.status === 'paused',
    ...(waitKind ? { wait_kind: waitKind } : {}),
  }
}

/** Resolve a server runtime snapshot, with one rolling-server fallback. */
export function goalPresentationFromRuntime(runtime, fallback = null) {
  if (runtime && Object.prototype.hasOwnProperty.call(runtime, 'goal')) {
    return normalizeGoalPresentation(runtime.goal)
  }
  const normalizedFallback = typeof fallback === 'string'
    ? normalizeGoalPresentation({ objective: fallback, status: 'active' })
    : normalizeGoalPresentation(fallback)
  if (!runtime?.running) return normalizedFallback
  const objective = compactGoalObjective(
    runtime.active_goal_objective || normalizedFallback?.objective,
  )
  return objective
    ? normalizeGoalPresentation({ objective, status: 'active' })
    : null
}

function hasResumableTail(message) {
  if (message?.role !== 'assistant' || !Array.isArray(message.blocks)) return false
  const tail = message.blocks[message.blocks.length - 1]
  return tail?.type === 'error' && tail.resumable === true
}

function previousVisibleMessageIndex(messages, beforeIndex) {
  for (let i = beforeIndex - 1; i >= 0; i -= 1) {
    if (!messages[i]?.hidden) return i
  }
  return -1
}

function priorGoalObjective(messages, beforeIndex) {
  for (let i = beforeIndex - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message?.role !== 'user' || message.hidden) continue
    if (isContinue(message.content)) continue
    return goalObjectiveFromText(message.content)
  }
  return ''
}

/**
 * Recover a goal when mounting into a turn that is already running.
 *
 * An ordinary attach reads the objective from the latest visible owner
 * message. A resumed goal instead starts with the synthetic owner message
 * "continue"; accept that as goal continuity only when it directly follows
 * the same resumable assistant tail that exposes the Resume action.
 */
export function latestGoalObjective(messages) {
  if (!Array.isArray(messages)) return ''
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message?.role !== 'user' || message.hidden) continue
    const directObjective = goalObjectiveFromText(message.content)
    if (directObjective) return directObjective
    if (!isContinue(message.content)) return ''
    const resumeTailIndex = previousVisibleMessageIndex(messages, i)
    if (resumeTailIndex < 0 || !hasResumableTail(messages[resumeTailIndex])) return ''
    return priorGoalObjective(messages, resumeTailIndex)
  }
  return ''
}

/**
 * Resolve the goal at the synchronous run-start seam.
 *
 * Before a one-tap Resume is appended, the resumable assistant note is still
 * the visible transcript tail. This mirrors latestGoalObjective's cold-attach
 * rule so live starts and reconnects cannot disagree about the active goal.
 */
export function goalObjectiveAtRunStart(text, messages) {
  const directObjective = goalObjectiveFromText(text)
  if (directObjective || !isContinue(text) || !Array.isArray(messages)) {
    return directObjective
  }
  const tailIndex = previousVisibleMessageIndex(messages, messages.length)
  if (tailIndex < 0 || !hasResumableTail(messages[tailIndex])) return ''
  return priorGoalObjective(messages, tailIndex)
}

/** Keep settled Goals visible across ordinary turns; reactivate only Resume. */
export function goalPresentationAtRunStart(text, messages, current = null) {
  const directObjective = goalObjectiveAtRunStart(text, messages)
  if (directObjective) {
    return normalizeGoalPresentation({
      objective: directObjective,
      status: 'active',
    })
  }
  const normalizedCurrent = normalizeGoalPresentation(current)
  if (isContinue(text) && normalizedCurrent?.status === 'paused') {
    return { ...normalizedCurrent, status: 'active', resumable: false }
  }
  return normalizedCurrent
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

/** Present the live execution owner rather than a stale optimistic task state. */
export function goalTaskDisplayStatus(task, execution) {
  if (!execution || execution.status === 'completed') return task?.status
  if (['starting', 'running', 'resuming', 'paused'].includes(execution.status)) {
    return 'running'
  }
  if (['failed', 'needs_review', 'interrupted'].includes(execution.status)) {
    return 'failed'
  }
  return execution.status
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
  const activeStatuses = new Set(['starting', 'running', 'resuming', 'paused'])
  const delegatedLeaves = []
  const collectDelegatedLeaves = (node, ancestors = new Set()) => {
    if (!node || ancestors.has(node.id)) return
    const branch = new Set(ancestors).add(node.id)
    const activeChildren = (node?.children || []).filter(child => (
      activeStatuses.has(child?.status) && !branch.has(child?.id)
    ))
    if (activeChildren.length) {
      activeChildren.forEach(child => collectDelegatedLeaves(child, branch))
    } else if (activeStatuses.has(node?.status)) {
      const title = String(node.task_key || '')
        .replace(/[._-]+/g, ' ')
        .replace(/^./, letter => letter.toUpperCase())
      delegatedLeaves.push({ id: node.id, title, status: 'running' })
    }
  }
  ;(goalPlan?.delegations || []).forEach(node => collectDelegatedLeaves(node))
  if (delegatedLeaves.length) return delegatedLeaves
  const tasks = Array.isArray(goalPlan?.tasks) ? goalPlan.tasks : []
  const running = tasks.filter(task => task?.status === 'running')
  if (running.length) return deepestPlanTasks(tasks, running)
  return deepestPlanTasks(tasks, tasks.filter(task => task?.ready === true))
}

export function progressRailViewModel(
  goal,
  buildPhases,
  goalPlan = null,
  waitState = null,
) {
  const items = []
  const presentation = typeof goal === 'string'
    ? normalizeGoalPresentation({ objective: goal, status: 'active' })
    : normalizeGoalPresentation(goal)
  const actionable = presentation && ['active', 'paused'].includes(presentation.status)
    ? presentation
    : null
  const goalObjective = actionable?.objective || ''
  if (goalObjective) {
    const completed = goalPlan?.summary?.completed
    const total = goalPlan?.summary?.total
    const planned = Number.isInteger(completed) && Number.isInteger(total)
    const activeTasks = visibleGoalTasks(goalPlan)
    const activeLabels = activeTasks.map(progressLabel).filter(Boolean)
    // The Goal lifecycle tells us whether the outcome is active; the chat
    // interaction tells us who owns the next move. Keep that distinction in
    // one existing rail instead of inventing a second persistent status card.
    const ownerActionRequired = waitState?.ownerActionRequired === true
    const monitoring = !ownerActionRequired && waitState?.monitoring === true
    const displayStatus = ownerActionRequired
      ? 'waiting for you'
      : monitoring
        ? 'monitoring'
        : actionable.status
    const statusLabel = ownerActionRequired
      ? 'Waiting for you'
      : monitoring
        ? 'Monitoring'
        : {
            paused: 'Paused',
            completed: 'Completed',
            failed: 'Needs attention',
          }[actionable.status]
    const progressSummary = planned ? `${completed}/${total}` : goalObjective
    items.push({
      key: 'goal',
      label: `Goal${statusLabel ? ` · ${statusLabel}` : ''} · ${progressSummary}${
        activeLabels.length
          ? ` · ${activeLabels.join(' + ')}`
          : ''
      }`,
      expandable: true,
      tone: actionable.status,
      ...(goalPlan ? {
        title: `Goal: ${goalObjective}`,
        ariaLabel: `Goal ${displayStatus} for ${goalObjective}; ${completed} of ${total} complete`,
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
