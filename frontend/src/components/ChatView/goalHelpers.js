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

/**
 * One row per helper this chat is still owed work from, for the foot's
 * helper list: background helpers first (they own this chat's next wake),
 * then this chat's own Goal helpers that are still working or asking their
 * coordinating agent, each helper once. Rows carry only what both sources
 * project (id, name, provider, status); a helper's live step and clock stay
 * on its transcript row until one live projection carries them.
 *
 * Only direct helpers are listed: a nested helper's conversation belongs to
 * the helper that started it, so it cannot be opened from this chat.
 * `cappedBackgroundCount` is the source total when its items were capped.
 * Goal rows may overlap omitted background helpers or be disjoint; neither
 * an exact union total nor an additional unseen count can be inferred.
 */
export function trayHelpers(backgroundHelpers, goal) {
  const rows = []
  const seen = new Set()
  const add = item => {
    if (!item?.id || seen.has(item.id)) return
    seen.add(item.id)
    rows.push({
      delegation_id: item.id,
      task_key: item.task_key || item.title || null,
      provider: item.provider || null,
      status: item.status || null,
    })
  }
  const items = backgroundHelpers?.items || []
  items.forEach(add)
  ;(goal?.plan?.delegations || [])
    .filter(node => isWorkingHelperStatus(node?.status) || isHelperAwaitingParent(node))
    .forEach(add)
  const count = Number(backgroundHelpers?.count) || 0
  const cappedBackgroundCount = count > items.length ? count : 0
  return { rows, cappedBackgroundCount }
}

/**
 * The Goal headline's helper clause: who acts next, never who merely exists.
 * A helper question is owed by its coordinating agent, so it never reads as
 * owner input; a saved owner card (`owner_input` handoff) outranks helper
 * work, which stays visible in the Goal details.
 */
export function goalHelperWaitingLabel(goal, { turnActive = false } = {}) {
  if (turnActive || !['active', 'paused'].includes(goal?.status)) return null
  if (goal.handoff?.kind === 'owner_input') return null
  const helpers = goalHelpers(goal)
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
