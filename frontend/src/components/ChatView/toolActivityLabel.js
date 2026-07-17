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
  // Image-viewing (owner ref 2026-07-17). Codex will emit ViewImage directly
  // once its ImageViewThreadItem is wired; a Claude image view is a Read of an
  // image file, mapped here by extension via effectiveToolName. Plural here —
  // the summary swaps to the singular "Viewing an image" for a lone one.
  ['ViewImage', 'Viewing images'],
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
  ['ViewImage', 'Viewed images'],
])

// Singular twins for a ONE-occurrence activity: a lone Bash reads "Ran a
// command", not "Ran commands" (the Codex idiom — owner ref 2026-07-17).
// Keyed by the PLURAL label so Read+Glob (both "Reading files") share one
// singular, and the summary swaps to it only when exactly one tool produced
// that label. Uncountable activities (code, the web, planning) have no entry
// and are invariant.
const PRESENT_SINGULAR = new Map([
  ['Running commands', 'Running a command'],
  ['Reading files', 'Reading a file'],
  ['Viewing images', 'Viewing an image'],
])
const PAST_SINGULAR = new Map([
  ['Ran commands', 'Ran a command'],
  ['Read files', 'Read a file'],
  ['Viewed images', 'Viewed an image'],
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
  ['ViewImage', 'image'],
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

// Given a PLURAL activity label, return its singular twin (or the label
// unchanged for an uncountable activity). The summaries call this only when a
// label was produced by exactly one tool in the stretch.
export function toolActivitySingular(label) {
  return PRESENT_SINGULAR.get(label) || label
}

export function toolActivityPastSingular(label) {
  return PAST_SINGULAR.get(label) || label
}

// The activity-relevant tool name for a tool block: a Read of an image file is
// an image VIEW ("Viewed an image" + picture glyph), not a file read. Claude
// surfaces both as the Read tool, so the only signal is the path's extension;
// a real ViewImage (Codex, once wired) passes straight through. Everything
// else returns the raw tool name unchanged. Takes the whole tool object
// because the classification needs its input, not just the name.
const IMAGE_PATH_RE = /\.(png|jpe?g|gif|webp|bmp|avif)(?:[?#].*)?$/i
export function effectiveToolName(tool) {
  const name = tool?.tool
  if (name === 'Read') {
    const path = tool?.input?.file_path || tool?.input?.path || ''
    if (typeof path === 'string' && IMAGE_PATH_RE.test(path)) return 'ViewImage'
  }
  return name
}
