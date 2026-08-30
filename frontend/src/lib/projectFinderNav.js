// Pure navigation model for the project Finder.
//
// A Finder "location" is a folder path plus an optionally-inspected file:
//   { path: '', selected: null }            the project root, nothing open
//   { path: 'src', selected: null }          inside src/, nothing open
//   { path: 'src', selected: 'src/a.md' }    inside src/, inspecting a.md
//
// Folder drill-downs and file inspections are BOTH forward steps: each pushes
// the previous location onto a back-stack and (in the component) registers one
// history-dismiss sentinel, so the browser Back button walks back through them
// in-tab instead of leaving the project. This module owns the pure stack
// transitions + breadcrumb math so the component only wires them to history.
//
// Dependency-free (no React, no DOM) so it is unit-testable in node:test.

export const FINDER_HOME = Object.freeze({ path: '', selected: null })

// The parent directory of a path, '' at the root.
export function parentPath(path) {
  const parts = String(path ?? '').split('/').filter(Boolean)
  parts.pop()
  return parts.join('/')
}

// Join a directory and a child name into a normalized project-relative path.
export function joinPath(dir, name) {
  return [dir, name].map(part => String(part ?? '')).filter(Boolean).join('/')
}

export function sameLocation(a, b) {
  return String(a?.path ?? '') === String(b?.path ?? '')
    && String(a?.selected ?? '') === String(b?.selected ?? '')
}

// Home-relative breadcrumb rows. The first crumb is the project root (labeled
// by `rootLabel`, path ''); each subsequent crumb is a path segment carrying the
// cumulative path so a click navigates straight to that ancestor.
export function finderCrumbs(rootLabel, path) {
  const parts = String(path ?? '').split('/').filter(Boolean)
  const crumbs = [{ label: rootLabel, path: '' }]
  let acc = ''
  for (const part of parts) {
    acc = acc ? `${acc}/${part}` : part
    crumbs.push({ label: part, path: acc })
  }
  return crumbs
}

// ── Back-stack transitions ───────────────────────────────────────────────────
//
// State is `{ current, stack }`. Every forward step pushes the old `current`
// onto `stack` (unless it's a true no-op) and returns whether a history sentinel
// must be opened. `back()` pops one entry. The component maps `pushed` to
// historyDismiss.open() and Back to back().

export function initFinder(location = FINDER_HOME) {
  return { current: { ...FINDER_HOME, ...location }, stack: [] }
}

// Navigate to a new location. Returns { state, pushed } where `pushed` is true
// iff the location actually changed (so exactly one history sentinel is opened
// per real forward step).
export function goTo(state, location) {
  const next = { path: String(location?.path ?? ''), selected: location?.selected ?? null }
  if (sameLocation(state.current, next)) return { state, pushed: false }
  return {
    state: { current: next, stack: [...state.stack, state.current] },
    pushed: true,
  }
}

// Open a folder: move into it, clearing any inspected file.
export function openFolder(state, path) {
  return goTo(state, { path: String(path ?? ''), selected: null })
}

// Inspect a file: keep the current folder, set the selected file.
export function openFile(state, filePath) {
  return goTo(state, { path: state.current.path, selected: String(filePath ?? '') })
}

// Close the inspected file, staying in the current folder.
export function closeFile(state) {
  return goTo(state, { path: state.current.path, selected: null })
}

// Pop one back-stack entry (the browser Back button, in-tab). At the bottom of
// the stack there is nothing to pop and `popped` is false — the component lets
// that Back bubble out of the Finder (leaving the project).
export function back(state) {
  if (state.stack.length === 0) return { state, popped: false }
  const stack = state.stack.slice(0, -1)
  const current = state.stack[state.stack.length - 1]
  return { state: { current, stack }, popped: true }
}

// Type-to-filter over one folder's entries. Case-insensitive substring on the
// entry name, prefix matches first, so Enter-opens-the-first-match lands on
// the obvious target ("re" jumps to readme.md before core.md). Order inside
// each tier keeps the listing's own directory-first, alphabetical shape.
export function filterEntries(entries, query) {
  const needle = String(query ?? '').trim().toLowerCase()
  if (!needle) return entries ?? []
  const starts = []
  const contains = []
  for (const entry of entries ?? []) {
    const name = String(entry.name ?? '').toLowerCase()
    if (name.startsWith(needle)) starts.push(entry)
    else if (name.includes(needle)) contains.push(entry)
  }
  return [...starts, ...contains]
}
