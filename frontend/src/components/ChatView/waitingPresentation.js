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

function helperTaskLabel(taskKey) {
  return String(taskKey || '')
    .replace(/[._-]+/g, ' ')
    .replace(/^./, letter => letter.toUpperCase())
}

export const RESOURCE_PAUSE_KINDS = new Set(['memory', 'storage'])

export function isResourcePause(block) {
  return !!block && RESOURCE_PAUSE_KINDS.has(block.pause?.kind)
}

export function resourcePausePresentation(block) {
  const kind = block?.pause?.kind
  const next = clockLabel(pauseTiming(block?.pause).checkAt)
  const storage = kind === 'storage'
  return {
    summary: storage
      ? 'Waiting for storage headroom'
      : 'Waiting for memory to settle',
    next: next ? `checks again ${next}` : 'checks again automatically',
    owner: 'Möbius resource monitor',
    pressure: storage
      ? 'Storage reached its measured critical boundary'
      : 'Memory reached its measured critical boundary',
    wakeUp: 'This chat resumes automatically when pressure clears',
    usage: 'No model tokens while waiting · one turn when it resumes',
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
  const latest = wait.latest_result?.summary || (wait.kind === 'timer'
    ? (wait.status === 'met' ? 'The timer finished.' : 'The timer has not finished yet.')
    : wait.status === 'met' ? 'The condition was met.'
      : wait.status === 'failed' ? 'The check could not finish.'
        : count ? 'The condition has not been met yet.' : 'Not checked yet.')

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
      latest: `${latest}${last ? ` · checked at ${last}` : ''}`,
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
    latest: `${latest}${last ? ` · checked at ${last}` : ''}`,
    timeoutLabel: activation ? 'Wake-up' : 'If it takes too long',
    timeout: activation
      ? 'A later ready restart wakes this chat; the Restart card has no time limit'
      : wait.kind === 'timer' ? (due ? `This chat resumes at ${due}` : 'This chat resumes when the timer finishes')
        : wait.deadline_at ? `This chat wakes to investigate at ${dateTimeLabel(wait.deadline_at)}`
          : 'No deadline recorded',
    usage: 'No model tokens while checking · one turn when it wakes',
  }
}

export function helperPresentation(backgroundHelpers) {
  const count = Number(backgroundHelpers?.count) || 0
  const tasks = (backgroundHelpers?.items || [])
    .map(item => helperTaskLabel(item?.task_key))
    .filter(Boolean)
  return {
    count,
    tasks,
    summary: `Waiting on ${count} ${count === 1 ? 'helper' : 'helpers'}`,
    owner: `${count} ${count === 1 ? 'helper agent' : 'helper agents'}`,
    usage: 'Helpers use their own turns · no separate monitor is polling',
  }
}
