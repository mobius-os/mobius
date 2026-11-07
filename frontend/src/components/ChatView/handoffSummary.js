/* What the foot's handoff stack shows when many helpers and waits are owed: one calm summary line, then rows grouped by who acts next. */
import { isHelperAwaitingParent, isWorkingHelperStatus } from './workingHelper.js'

// Up to this many rows (helpers plus waits) show as themselves. On a 426px
// phone that is about 125px above the composer, under 15% of the screen; one
// more row already reads as a list, so past it the foot shows one summary line.
export const HANDOFF_ROW_LIMIT = 3

const OWNER_RESUME_BLOCKERS = new Set(['manual_resume', 'resume_failed', 'owner_input', 'restart'])

/** A wait whose saved result is held until the owner acts (the "Needs you" card label). */
export function waitNeedsOwner(wait) {
  return !!wait?.delivery_pending && OWNER_RESUME_BLOCKERS.has(wait.resume_blocker)
}

/** One helper row's state, in who-acts-next terms. */
export function helperState(helper) {
  if (isHelperAwaitingParent(helper)) return 'asking'
  if (helper?.status === 'paused') return 'on_hold'
  if (isWorkingHelperStatus(helper?.status)) return 'working'
  if (helper?.status === 'completed') return 'finished'
  return 'stopped'
}

export function shouldSummarizeHandoffs({ helpers = [], waits = [], unlistedHelpers = 0 }) {
  return unlistedHelpers > 0 || helpers.length + waits.length > HANDOFF_ROW_LIMIT
}

const GROUPS = [
  { key: 'needs-you', label: 'Needs you' },
  { key: 'working', label: 'Working' },
  { key: 'waiting', label: 'Waiting' },
  { key: 'finished', label: 'Finished' },
]
const HELPER_GROUP = { working: 'working', on_hold: 'working', asking: 'waiting', finished: 'finished', stopped: 'finished' }

/**
 * Rows grouped by who acts next: the owner, then working helpers, then work
 * waiting on someone else (a coordinating agent or an external check), then
 * settled helpers whose results this chat has yet to take. Empty groups are
 * left out. Rows are flat today; a nested helper can later ride in its
 * root's group, indented under it, without a new group.
 */
export function handoffGroups({ helpers = [], waits = [] }) {
  const byKey = new Map(GROUPS.map(group => [group.key, { ...group, helpers: [], waits: [] }]))
  for (const helper of helpers) byKey.get(HELPER_GROUP[helperState(helper)]).helpers.push(helper)
  for (const wait of waits) byKey.get(waitNeedsOwner(wait) ? 'needs-you' : 'waiting').waits.push(wait)
  return [...byKey.values()]
    .map(group => ({ ...group, count: group.helpers.length + group.waits.length }))
    .filter(group => group.count > 0)
}

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`

/** "1 needs you · 12 helpers working · 2 waiting on coordinator · 3 waits". */
export function handoffSummaryLabel({ helpers = [], waits = [], unlistedHelpers = 0 }) {
  const helperCounts = { working: 0, on_hold: 0, asking: 0, finished: 0, stopped: 0 }
  for (const helper of helpers) helperCounts[helperState(helper)] += 1
  const needsYou = waits.filter(waitNeedsOwner).length
  const otherWaits = waits.length - needsYou
  return [
    needsYou ? `${needsYou} needs you` : null,
    helperCounts.working ? `${plural(helperCounts.working, 'helper', 'helpers')} working` : null,
    helperCounts.on_hold ? `${helperCounts.on_hold} on hold` : null,
    helperCounts.asking ? `${helperCounts.asking} waiting on coordinator` : null,
    otherWaits ? plural(otherWaits, 'wait', 'waits') : null,
    helperCounts.finished ? `${helperCounts.finished} finished` : null,
    helperCounts.stopped ? `${helperCounts.stopped} stopped` : null,
    unlistedHelpers ? plural(unlistedHelpers, 'more helper', 'more helpers') : null,
  ].filter(Boolean).join(' · ')
}
