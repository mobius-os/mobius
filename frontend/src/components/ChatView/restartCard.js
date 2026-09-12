/** UI-only validation for the platform-owned restart card wire shape. */
import { questionOptionSubmission } from './questionSubmission.js'
export function isRestartCardAction(action) {
  return action?.type === 'restart' && [1, 2].includes(action?.version)
}


/**
 * Resolve the card's displayed labels back to the server-issued option ids.
 * Free text and unknown ids fail closed; the backend remains authoritative.
 */
export function restartCardSelectedOptions(action, questions, answers) {
  if (!isRestartCardAction(action)) return undefined
  if (typeof action.restart_option_id !== 'string' || !action.restart_option_id) {
    return null
  }
  const allowed = new Set([
    action.restart_option_id,
    ...(action.version === 1 ? [action.cancel_option_id] : []),
  ].filter(value => typeof value === 'string' && value))
  const { selected_options: selected } = questionOptionSubmission(questions || [], answers)
  const ids = Object.values(selected)
  // Writing a response is deliberately not restart authority. It carries no
  // saved option id, so the backend can continue the conversation while the
  // exact Restart now identity remains the only path to the side effect.
  if (ids.length === 0) return selected
  return ids.length === 1 && ids[0].length === 1 && allowed.has(ids[0][0])
    ? selected : null
}


export function restartCardStatusLabel(action) {
  if (!isRestartCardAction(action)) return ''
  return ({
    restart_requested: 'Restart requested',
    activated: 'Changes loaded',
    deferred: 'Waiting for a later restart',
    responded: 'Response sent',
    dismissed: 'Restart wait cancelled',
    expired: 'Restart request closed',
    uncertain: 'Restart outcome needs review',
    activation_uncertain: 'Restart outcome needs review',
    failed: 'Restart not completed',
  })[action.status] || ''
}


export function restartCardStatusDetail(action) {
  if (!isRestartCardAction(action)) return ''
  return ({
    restart_requested: 'Möbius is draining active work and will load these exact changes once.',
    activated: 'A ready server has verified that these exact changes are loaded.',
    deferred: 'The changes remain linked and can load with a matching restart later.',
    responded: 'This card did not restart Möbius. The agent will respond to what you wrote instead.',
    dismissed: 'This card cannot restart Möbius. The agent can check the current changes and ask again if needed.',
    expired: 'Nothing was restarted from this card. The agent can check whether a restart is still needed and ask again.',
    uncertain: 'Nothing will be replayed automatically. The agent will check the current state before asking again.',
    activation_uncertain: 'Nothing will be replayed automatically. The agent will check the current state before asking again.',
    failed: 'Nothing will be replayed automatically. The agent will check the current state before asking again.',
  })[action.status] || ''
}
