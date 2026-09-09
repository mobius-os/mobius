/** UI-only validation for the platform-owned restart card wire shape. */
import { questionOptionSubmission } from './questionSubmission.js'
export function isRestartCardAction(action) {
  return action?.type === 'restart' && action?.version === 1
}


/**
 * Resolve the card's displayed labels back to the server-issued option ids.
 * Free text and unknown ids fail closed; the backend remains authoritative.
 */
export function restartCardSelectedOptions(action, questions, answers) {
  if (!isRestartCardAction(action)) return undefined
  const allowed = new Set([
    action.restart_option_id,
    action.cancel_option_id,
  ].filter(value => typeof value === 'string' && value))
  const { selected_options: selected } = questionOptionSubmission(questions || [], answers)
  const ids = Object.values(selected)
  return ids.length === 1 && ids[0].length === 1 && allowed.has(ids[0][0])
    ? selected : null
}


export function restartCardStatusLabel(action) {
  if (!isRestartCardAction(action)) return ''
  return ({
    restart_requested: 'Restart requested',
    activated: 'Changes loaded',
    deferred: 'Waiting for a later restart',
    dismissed: 'Restart wait cancelled',
    uncertain: 'Restart outcome needs review',
    activation_uncertain: 'Restart outcome needs review',
    failed: 'Restart not completed',
  })[action.status] || ''
}
