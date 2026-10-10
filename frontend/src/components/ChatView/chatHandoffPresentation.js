/* Current responsibility and exact Goal continuation without changing lifecycle state. */
import { canResumeGoal, goalStatusLabel } from './goalProgress.js'

const TERMINAL_GOALS = new Set(['completed', 'cannot_complete', 'cancelled'])

export function currentProgressGoal(goal, { turnActive = false, continuingGoalId = null } = {}) {
  if (!goal || TERMINAL_GOALS.has(goal.status)) return null
  // A retained hold is history while an unrelated owner request is running.
  const waiting = ['automatic', 'owner_input'].includes(goal.handoff?.kind)
  if (turnActive && goal.status === 'paused' && !waiting && continuingGoalId !== goal.id) return null
  return goal
}

export function goalContinuationHandoff(goal, {
  turnActive = false,
  hasPendingQuestion = false,
  hasPendingResume = false,
  chatHandoff = 'none',
} = {}) {
  if (hasPendingResume || !canResumeGoal(goal, { turnActive, hasPendingQuestion, chatHandoff })) return null
  const deferred = goal.pause_reason === 'deferred'
  const interrupted = !['deferred', 'owner', 'agent'].includes(goal.pause_reason)
  return {
    description: deferred ? goal.hold_reason || 'The remaining work was deliberately deferred.'
      : interrupted ? 'The work was interrupted. Its outcome is not complete.'
        : 'The work is saved and will not continue automatically.',
    actionLabel: deferred ? 'Continue this work' : 'Resume this work',
    boundary: deferred
      ? 'Continuing reopens this work. It does not approve a previously declined action.'
      : 'Resume continues this exact Goal; unrelated work is not reopened.',
  }
}

export function currentChatAnnouncement({ turnActive, hasPendingQuestion, chatHandoff, goal, recoveryStatus, hasAnswer }) {
  if (hasPendingQuestion || chatHandoff === 'owner_input') return 'Waiting for you. Answer the saved card to continue.'
  if (turnActive || chatHandoff === 'working') return 'Assistant is working.'
  if (chatHandoff === 'automatic') return 'Waiting. This chat will continue automatically.'
  if (recoveryStatus) return recoveryStatus
  if (goal?.status === 'paused' && goal.pause_reason === 'deferred') return 'On hold. Continue this work whenever you choose.'
  if (goal?.status === 'paused') return `${goalStatusLabel(goal)}. Resume is available.`
  // Terminal Goal outcomes belong to their transcript receipt, not the next request.
  return hasAnswer ? 'Response ready.' : ''
}
