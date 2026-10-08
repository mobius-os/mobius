/* Goal-owned helper presentation comes only from the embedded plan tree. */
const UNSETTLED = new Set(['accepted', 'retrying', 'starting', 'running', 'resuming', 'paused', 'needs_input'])

export function goalHelpers(goal) {
  const helpers = []
  const seen = new Set()
  const visit = (node, inheritedTask = null) => {
    if (!node || seen.has(node.id)) return
    seen.add(node.id)
    const planTask = node.plan_task || inheritedTask
    helpers.push({ ...node, plan_task: planTask })
    ;(node.children || []).forEach(child => visit(child, planTask))
  }
  ;(goal?.plan?.delegations || []).forEach(node => visit(node))
  return helpers
}

/** Keep unrelated (or unproven) background work in its own Waiting panel. */
export function helpersOutsideGoal(backgroundHelpers, goal) {
  const owned = new Set(goalHelpers(goal).map(node => node.id))
  const items = backgroundHelpers?.items || []
  const matched = items.filter(item => owned.has(item.id)).length
  return {
    count: Math.max(0, (backgroundHelpers?.count || 0) - matched),
    items: items.filter(item => !owned.has(item.id)),
  }
}

export function goalHelperWaitingLabel(goal, { turnActive = false } = {}) {
  if (turnActive || !['active', 'paused'].includes(goal?.status)) return null
  const helpers = goalHelpers(goal).filter(node => UNSETTLED.has(node.status))
  if (!helpers.length) return null
  const waiting = `Waiting on ${helpers.length} ${helpers.length === 1 ? 'helper' : 'helpers'}`
  if (helpers.some(node => node.status === 'needs_input')) return `${waiting} · Needs an answer`
  if (goal.status === 'paused' && goal.handoff?.kind !== 'automatic') return `On hold · ${waiting}`
  return goal.handoff?.kind === 'automatic'
    ? `${waiting} · resumes automatically`
    : waiting
}
