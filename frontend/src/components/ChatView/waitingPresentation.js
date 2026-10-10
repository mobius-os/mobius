/* Presentation shared by active handoffs and settled wait history. */
import { formatDateTime, formatTime } from '../../lib/dateTimeFormat.js'
import { pauseTiming } from './resetTime.js'

export function waitConditionLabel(description) {
  // Sentence-case the instruction, not case-sensitive project names or refs.
  return String(description || '').trim().replace(/^resume when\b/, 'Resume when')
}

function apiDate(value) {
  if (!value) return null
  const text = String(value)
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(text) ? text : `${text}Z`)
  return Number.isNaN(date.getTime()) ? null : date
}

function clockLabel(value) {
  return formatTime(apiDate(value)) || null
}

function dateTimeLabel(value) {
  return formatDateTime(apiDate(value)) || 'not set'
}

function cadenceLabel(wait) {
  if (wait.kind === 'timer') return 'one-time timer'
  const seconds = Number(wait.interval_secs) || 300
  if (seconds < 120) return 'every minute'
  if (seconds < 3600) return `every ${Math.round(seconds / 60)} minutes`
  const hours = Math.round(seconds / 3600)
  return `every ${hours} ${hours === 1 ? 'hour' : 'hours'}`
}

export const RESOURCE_PAUSE_KINDS = new Set(['memory', 'storage'])

export function isResourcePause(block) {
  return !!block && RESOURCE_PAUSE_KINDS.has(block.pause?.kind)
}

export function resourcePausePresentation(block, handoff = null) {
  const kind = block?.pause?.kind
  const next = clockLabel(pauseTiming(block?.pause).checkAt)
  const storage = kind === 'storage'
  const memory = kind === 'memory'
  const limit = ['rate_limit', 'usage_limit', 'limit'].includes(kind)
  const automatic = handoff?.kind === 'automatic' && handoff.reason === kind
  const restoring = handoff?.kind === 'automatic' && handoff.reason === 'restoring_edits'
  const restart = handoff?.kind === 'recovery' && handoff.reason === 'restart_required'
  const blocked = handoff && handoff.kind !== 'automatic'
  const manual = !restoring && (!!blocked || ((kind === 'model_capacity' || limit) && !automatic))
  return {
    manual,
    summary: storage ? 'Waiting for storage headroom'
      : memory ? 'Waiting for memory to settle'
        : limit ? 'Provider usage limit' : 'Model capacity',
    next: restoring ? 'restoring local work'
      : restart ? 'server restart needed'
      : handoff?.kind === 'owner_input' ? 'answer needed'
      : manual
      ? 'manual recovery' : next ? `checks again ${next}` : 'checks again automatically',
    owner: storage || memory ? 'Möbius resource monitor' : 'Provider availability',
    pressure: storage ? 'Storage reached its measured critical boundary'
      : memory ? 'Memory reached its measured critical boundary'
        : limit ? 'Provider usage limit reached' : 'Model capacity unavailable',
    wakeUp: restoring ? 'Möbius is restoring local work; this chat continues after that work is restored and loaded'
      : restart ? 'Waiting for a server restart to load restored work; use the saved Restart card'
      : blocked && handoff.kind === 'owner_input' ? 'Answer the saved card before this chat can continue'
      : blocked && ['memory', 'storage'].includes(kind) ? 'Automatic resource recovery is blocked; inspect the saved recovery card'
      : kind === 'model_capacity' && !automatic ? 'Automatic retries are unavailable; choose another model and Resume'
      : limit && !automatic ? handoff?.reason === 'manual_resume'
        ? 'Enable automatic continuation or choose another model and Resume'
        : 'Automatic continuation is blocked; inspect the saved recovery card'
        : kind === 'model_capacity' ? 'Möbius retries this model automatically while eligible'
          : limit ? 'Automatic continuation is enabled; this chat retries when eligible'
          : 'This chat resumes automatically when pressure clears',
    usage: manual
      ? 'No model tokens while parked · one turn after recovery'
      : 'No model tokens while waiting · one turn when it resumes',
  }
}

const RESUME_BLOCKERS = {
  platform_restart: ['waiting for platform restart', 'Complete the pending platform restart; this chat resumes after restored code loads'],
  restoring_edits: ['restoring local work', 'Möbius is restoring local work before this chat can resume'],
  owner_input: ['waiting for your answer', 'Answer the saved card in this chat; the result remains saved until then'],
  restart: ['waiting for restart', 'Use the pending Restart card; a ready restart releases this follow-up'],
  provider_park: ['waiting for agent availability', 'The existing agent recovery hold must clear before this chat can resume'],
  manual_resume: ['waiting for Resume', 'Resume this chat to release its manual recovery hold'],
  live_turn: ['waiting for current turn', 'The result is saved while this chat finishes its current turn'],
  resume_failed: ['follow-up needs attention', 'The follow-up could not start successfully; resume this chat to inspect the saved result'],
}

export function waitPresentation(wait) {
  const activation = wait.kind === 'platform_activation'
  const cadence = cadenceLabel(wait)
  const next = clockLabel(wait.next_check_at)
  const due = clockLabel(wait.due_at)
  const count = Number(wait.checks_count) || 0
  const last = clockLabel(wait.last_checked_at)
  const activity = count
    ? `${count} ${count === 1 ? 'check' : 'checks'}${last ? ` · last at ${last}` : ''}`
    : 'Not checked yet'
  const summary = wait.kind === 'timer'
    ? (due ? `resumes ${due}` : 'resumes later')
    : (next ? `next check ${next}` : cadence)

  if (wait.delivery_pending) {
    const outcome = wait.status === 'failed' ? 'Check failed'
      : wait.status === 'expired' ? 'Check reached its deadline'
      : activation ? 'Restart confirmed'
      : wait.kind === 'timer' ? 'Timer finished' : 'Checks finished'
    const [summary, wakeUp] = RESUME_BLOCKERS[wait.resume_blocker]
      || ['follow-up pending', 'The result is saved until this chat can resume']
    return {
      condition: `${outcome}; ${summary}`,
      owner: wait.condition_owner || (wait.kind === 'timer' ? 'Time' : 'External system'),
      summary: 'result saved',
      checker: 'Finished · no more checks',
      activity,
      timeoutLabel: 'Next step',
      timeout: wakeUp,
      usage: 'No model tokens while blocked · one turn when the result is delivered',
    }
  }

  return {
    condition: waitConditionLabel(wait.description) || 'External condition',
    owner: wait.condition_owner || (wait.kind === 'timer' ? 'Time' : 'External system'),
    summary,
    checker: `Möbius · ${cadence}${wait.kind !== 'timer' && next ? ` · next at ${next}` : ''}`,
    activity,
    timeoutLabel: activation ? 'Wake-up' : 'If it takes too long',
    timeout: activation
      ? 'A later ready restart wakes this chat; the Restart card has no time limit'
      : `This chat wakes to investigate at ${dateTimeLabel(wait.deadline_at)}`,
    usage: 'No model tokens while checking · one turn when it wakes',
  }
}

export function helperPresentation(backgroundHelpers, handoff = null) {
  const count = Number(backgroundHelpers?.count) || 0
  const tasks = (backgroundHelpers?.items || [])
    .map(item => item?.title || 'Helper work')
    .filter(Boolean)
  return {
    count,
    tasks,
    summary: `Waiting on ${count} ${count === 1 ? 'helper' : 'helpers'}`,
    owner: `${count} ${count === 1 ? 'helper agent' : 'helper agents'}`,
    automatic: handoff?.kind === 'automatic',
    usage: 'Usage unknown',
  }
}
