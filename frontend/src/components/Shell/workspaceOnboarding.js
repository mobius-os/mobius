// Keyboard-undo gating for the workspace drag feature (design §3.5). Pure,
// dependency-free helpers keep Shell's global shortcut declarative and tested
// without a DOM.

// Cmd/Ctrl+Z (no Shift, no Alt) — the workspace-undo chord (design §3.5). Shift
// is excluded so it never steals redo.
export function undoKeyPressed(e) {
  if (!e || (!e.metaKey && !e.ctrlKey) || e.shiftKey || e.altKey) return false
  const k = typeof e.key === 'string' ? e.key.toLowerCase() : ''
  return k === 'z'
}

// True when focus is in a text-entry surface, so the global undo chord defers to
// the field's own editing (design §3.5: "while no input is focused").
export function isEditableTarget(el) {
  if (!el || typeof el !== 'object') return false
  const tag = typeof el.tagName === 'string' ? el.tagName.toLowerCase() : ''
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true
  return el.isContentEditable === true
}
