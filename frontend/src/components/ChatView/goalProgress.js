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

function isContinue(text) {
  return typeof text === 'string' && text.trim().toLowerCase() === 'continue'
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

/** Keep a known goal through a rolling/recovered active runtime; idle ends it. */
export function goalObjectiveFromRuntime(runtime, fallbackObjective = '') {
  if (!runtime?.running) return ''
  return runtime.active_goal_objective || fallbackObjective || ''
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
