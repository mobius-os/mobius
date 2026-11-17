/* Goal-owned helper presentation comes only from the embedded plan tree. */
import { isHelperAwaitingParent, isWorkingHelperStatus } from './workingHelper.js'

const helperCount = count => `${count} ${count === 1 ? 'helper' : 'helpers'}`

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

/** Select the server-deduplicated, bounded view for the currently shown Goal.
 * Never union a capped source in the browser: omitted membership is unknown.
 */
export function trayHelpers(backgroundHelpers, goal) {
  const projected = goal?.id && backgroundHelpers?.with_goal?.goal_id === goal.id
    ? backgroundHelpers.with_goal : backgroundHelpers
  const rows = (projected?.items || []).map(item => ({
    delegation_id: item.id,
    task_key: item.task_key || null,
    title: item.title || null,
    provider: item.provider || null,
    status: item.status || null,
  }))
  return { rows, unlistedHelpers: Math.max(0, (projected?.count || 0) - rows.length) }
}

/**
 * The Goal headline's helper clause: who acts next, never who merely exists.
 * A helper question is owed by its coordinating agent, so it never reads as
 * owner input; a saved owner card (`owner_input` handoff) outranks helper
 * work, which stays visible in the Goal details.
 */
export function goalHelperWaitingLabel(goal, { turnActive = false, directHelpersInTray = false } = {}) {
  if (turnActive || !['active', 'paused'].includes(goal?.status)) return null
  if (goal.handoff?.kind === 'owner_input') return null
  const direct = new Set(directHelpersInTray ? (goal.plan?.delegations || []).map(node => node.id) : [])
  const helpers = goalHelpers(goal).filter(node => !direct.has(node.id))
  const working = helpers.filter(node => isWorkingHelperStatus(node.status)).length
  const asking = helpers.filter(isHelperAwaitingParent).length
  if (!working && !asking) return null
  const waiting = [
    working ? `Waiting on ${helperCount(working)}` : null,
    asking
      ? `${working ? asking : asking === 1 ? 'Helper' : helperCount(asking)} waiting for coordinating agent`
      : null,
  ].filter(Boolean).join(' · ')
  if (goal.status === 'paused' && goal.handoff?.kind !== 'automatic') return `On hold · ${waiting}`
  return goal.handoff?.kind === 'automatic'
    ? `${waiting} · resumes automatically`
    : waiting
}
