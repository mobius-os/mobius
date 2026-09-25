/* Which step of a drawn timeline launched each Möbius helper, so every helper is one row where it began. */
import { effectiveToolName } from './toolActivityLabel.js'

const HELPER_ID_RE = /"helper_id"\s*:\s*"([^"]+)"/

// What a spawn_agent call started: the exact helper its result names, or —
// while the call is still in flight, or in an older record — the name it asked
// for. A launch that failed started nothing; it stays an ordinary failed step.
function launchedBy(item) {
  if (item?.type !== 'tool' || effectiveToolName(item) !== 'HelperSpawn') return null
  if (item.status === 'error' || (item.output_exit_code ?? 0) !== 0) return null
  const id = typeof item.output === 'string' ? item.output.match(HELPER_ID_RE)?.[1] : null
  if (id) return { id }
  const name = typeof item.input === 'string' ? item.input.trim() : ''
  return name ? { name } : null
}

/**
 * Pair each successful launch step with the helper it started. Returns
 * `rowAt` (launch step item → helper row event) and `drawnAtLaunch` (the helper
 * events those rows already stand for, which then draw nothing at their own
 * recorded position). Exact helper ids pair first, so a retried launch with the
 * same name can never claim — or duplicate — another launch's helper.
 */
export function helperLaunches(entries = []) {
  const helpers = entries.map(entry => entry?.item).filter(item => item?.type === 'helper_result')
  const launches = entries
    .map(entry => [entry?.item, launchedBy(entry?.item)])
    .filter(([, launch]) => launch)
  const rowAt = new Map()
  const drawnAtLaunch = new Set()
  const pair = (item, helper) => {
    if (!helper) return
    rowAt.set(item, helper)
    drawnAtLaunch.add(helper)
  }
  for (const [item, { id }] of launches) {
    if (id) pair(item, helpers.find(helper => helper.delegation_id === id && !drawnAtLaunch.has(helper)))
  }
  for (const [item, { name }] of launches) {
    if (name) pair(item, helpers.find(helper => helper.task_key === name && !drawnAtLaunch.has(helper)))
  }
  return { rowAt, drawnAtLaunch }
}
