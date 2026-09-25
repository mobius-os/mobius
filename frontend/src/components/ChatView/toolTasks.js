// A tool block's `.subagent` map records every provider task the tool started
// (streamReducers.applyTaskEvent live, backend events.py when persisted). Two
// different things arrive through that one channel:
//
//   - AGENT helpers (Claude's Agent/Task fleet, Codex collab agents) — they have
//     their own conversation and render as helper rows with a running/done count;
//   - SHELL tasks (Claude's local_bash for every Bash call, the Monitor tool) —
//     they are the command itself, not a helper. They never become a row; a
//     shell task still running after its tool call returned means the command
//     was sent (or auto-moved) to the background, which the command row itself
//     shows with a live timer.
//
// This module is the one place that tells them apart.

const SHELL_TASK_TYPES = new Set(['local_bash', 'monitor'])
// Rows persisted before task_type was recorded carry only their id. Claude
// names every background shell task `b` + 8 characters and every agent `a` +
// 16 hex, so that shape alone separates them in older history.
const LEGACY_SHELL_TASK_ID = /^b[a-z0-9]{8}$/

function isShellTask(taskId, task) {
  if (task?.task_type) return SHELL_TASK_TYPES.has(task.task_type)
  return LEGACY_SHELL_TASK_ID.test(String(taskId))
}

// A malformed persisted entry (e.g. {t1: null}) must not crash a render.
function taskEntries(tool) {
  const map = tool?.subagent
  if (!map || typeof map !== 'object') return []
  return Object.entries(map).filter(([, task]) => task && typeof task === 'object')
}

// The agent helpers a tool started, as [taskId, helper] pairs.
export function agentHelperEntries(tool) {
  return taskEntries(tool).filter(([taskId, task]) => !isShellTask(taskId, task))
}

// The shell task still running in the background after its tool call settled,
// or null. A foreground command that is still running is just "running": its
// row already says so, and only background work earns the extra timer.
export function runningBackgroundTask(tool) {
  if (!tool || tool.status === 'running') return null
  const found = taskEntries(tool).find(
    ([taskId, task]) => task.status === 'running' && isShellTask(taskId, task),
  )
  return found ? found[1] : null
}

// Whole-second elapsed, compact ("8s", "1m 04s").
export function elapsedLabel(ms) {
  const total = Math.max(0, Math.round(ms / 1000))
  if (total < 60) return `${total}s`
  const mins = Math.floor(total / 60)
  const secs = total % 60
  return `${mins}m ${String(secs).padStart(2, '0')}s`
}
