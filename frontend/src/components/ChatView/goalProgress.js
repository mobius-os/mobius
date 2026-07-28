/* Goal-command parsing and the shared footer progress-rail view model. */

/**
 * Return the objective carried by a real leading `/goal` command.
 *
 * The backend intentionally recognizes commands only at character zero (with
 * leading newlines tolerated), so the indicator follows that same boundary
 * instead of lighting up for ordinary prose that happens to mention `/goal`.
 * Whitespace is collapsed because the footer is a one-line status surface.
 */
export function goalObjectiveFromText(text) {
  if (typeof text !== 'string') return ''
  const normalized = text.replace(/^\n+/, '')
  const match = normalized.match(/^\/goal(?:[ \t]+([\s\S]*))?$/)
  if (!match) return ''
  const objective = (match[1] || '').trim().replace(/\s+/g, ' ')
  if (!objective || objective.toLowerCase() === 'clear') return ''
  return objective
}

/**
 * Recover a goal when mounting into a turn that is already running.
 *
 * The latest visible owner message is the run-start message on an ordinary
 * attach. Live steers do not replace the in-memory goal state; this fallback is
 * only for a fresh mount/reconnect where that local state does not yet exist.
 */
export function latestGoalObjective(messages) {
  if (!Array.isArray(messages)) return ''
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (message?.role !== 'user' || message.hidden) continue
    return goalObjectiveFromText(message.content)
  }
  return ''
}

/**
 * Put the active goal and ordinary build phases on one existing progress rail.
 *
 * The last item is current: before a build phase arrives that is the goal
 * itself; afterwards the goal remains as quiet context while the newest phase
 * carries emphasis.
 */
export function progressRailViewModel(goalObjective, buildPhases) {
  const items = []
  if (goalObjective) {
    items.push({
      key: 'goal',
      label: `Goal · ${goalObjective}`,
      expandable: true,
    })
  }
  for (const phase of Array.isArray(buildPhases) ? buildPhases : []) {
    if (!phase?.label) continue
    items.push({
      key: `phase-${phase.ts}`,
      label: phase.label,
    })
  }
  const lastIndex = items.length - 1
  return items.map((item, index) => ({
    ...item,
    current: index === lastIndex,
  }))
}
