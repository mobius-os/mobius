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
  ['Task', 'Working in the background'],
  ['Agent', 'Working in the background'],
  ['AskUserQuestion', 'Asking you'],
  ['Skill', 'Using a skill'],
])

// An unknown tool falls back to its raw name (then the generic 'Tool' for a
// missing name), so a new tool degrades to today's rendering, never a crash.
export function toolActivityLabel(name) {
  return ACTIVITY_LABELS.get(name) || name || 'Tool'
}
