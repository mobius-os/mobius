/* Goal-owned helper presentation comes only from the embedded plan tree. */

export function goalHelpers(goal) {
  const helpers = []
  const seen = new Set()
  const visit = node => {
    if (!node || seen.has(node.id)) return
    seen.add(node.id)
    helpers.push(node)
    ;(node.children || []).forEach(visit)
  }
  ;(goal?.plan?.delegations || []).forEach(visit)
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
