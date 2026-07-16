// Owner-facing activity labels for raw tool names. Collapsed summary lines
// (the activity-group header, a running tool's header) speak in activities —
// "Reading files", not "Read"/"Glob" — because tool names are implementation
// vocabulary the owner shouldn't need. The expanded detail view keeps the raw
// tool name + input, so exactly what ran stays inspectable.
//
// A Map, not an object literal: lookups must never walk the prototype chain
// (a tool named "constructor" would otherwise resolve to a function).
const ACTIVITY_LABELS = new Map([
  ['Read', 'Reading files'],
  ['Glob', 'Reading files'],
  ['Grep', 'Searching the code'],
  ['Edit', 'Editing code'],
  ['Write', 'Editing code'],
  ['MultiEdit', 'Editing code'],
  ['NotebookEdit', 'Editing code'],
  ['Bash', 'Running commands'],
  ['WebFetch', 'Browsing the web'],
  ['WebSearch', 'Browsing the web'],
  ['TodoWrite', 'Planning'],
  ['ToolSearch', 'Planning'],
  ['Task', 'Working in the background'],
  ['Agent', 'Working in the background'],
  ['Workflow', 'Working in the background'],
  ['TaskOutput', 'Working in the background'],
  ['AskUserQuestion', 'Asking you'],
  ['Skill', 'Using a skill'],
])

// Past-tense twins for SETTLED lines — "Ran commands", not a "Running
// commands" frozen in time (the Codex idiom the owner asked for, 2026-07-16).
// The progressive map above stays the voice of anything still live.
const PAST_LABELS = new Map([
  ['Read', 'Read files'],
  ['Glob', 'Read files'],
  ['Grep', 'Searched the code'],
  ['Edit', 'Edited code'],
  ['Write', 'Edited code'],
  ['MultiEdit', 'Edited code'],
  ['NotebookEdit', 'Edited code'],
  ['Bash', 'Ran commands'],
  ['WebFetch', 'Browsed the web'],
  ['WebSearch', 'Browsed the web'],
  ['TodoWrite', 'Planned'],
  ['ToolSearch', 'Planned'],
  ['Task', 'Worked in the background'],
  ['Agent', 'Worked in the background'],
  ['Workflow', 'Worked in the background'],
  ['TaskOutput', 'Worked in the background'],
  ['AskUserQuestion', 'Asked you'],
  ['Skill', 'Used a skill'],
])

// A small muted type glyph keyed off the FIRST activity in a settled line
// (terminal for commands, magnifier for search, …) — ActivityStretch maps
// these keys to inline SVGs. Type icons are informative structure, unlike a
// success checkmark, so they don't violate the no-success-iconography rule.
const ACTIVITY_ICONS = new Map([
  ['Read', 'files'],
  ['Glob', 'files'],
  ['Grep', 'search'],
  ['Edit', 'edit'],
  ['Write', 'edit'],
  ['MultiEdit', 'edit'],
  ['NotebookEdit', 'edit'],
  ['Bash', 'terminal'],
  ['WebFetch', 'web'],
  ['WebSearch', 'web'],
  ['TodoWrite', 'plan'],
  ['ToolSearch', 'plan'],
  ['AskUserQuestion', 'dot'],
  ['Skill', 'dot'],
])

// An unknown tool falls back to its raw name (then the generic 'Tool' for a
// missing name), so a new tool degrades to today's rendering, never a crash.
export function toolActivityLabel(name) {
  return ACTIVITY_LABELS.get(name) || name || 'Tool'
}

// Past-tense label, or null for a tool outside the map. The null (rather than
// the raw-name fallback) lets the summary joiner know the label is a plain
// English phrase it may lowercase mid-sentence — a raw tool name keeps its
// casing and is substituted by the caller.
export function toolActivityPastLabel(name) {
  return PAST_LABELS.get(name) || null
}

export function toolActivityIcon(name) {
  return ACTIVITY_ICONS.get(name) || 'dot'
}
