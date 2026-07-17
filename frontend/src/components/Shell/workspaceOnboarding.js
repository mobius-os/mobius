// Onboarding + keyboard-undo gating for the workspace drag feature (design §7 /
// §3.5). Pure, dependency-free helpers so the arming/insertion/keymatch logic is
// unit-tested without a DOM, and Shell/WalkthroughOverlay stay declarative.

// localStorage key: the first-use coachmark shows at most once per browser.
export const HINT_KEY = 'mobius:hint-workspace'

// The coachmark arms when the workspace first holds ≥2 tabs, the splits flag is
// on, and it has not already been dismissed this browser (design §7.2). It is
// deliberately NOT gated on a pointer event — an unrelated first tap must never
// kill it unread, so the caller dismisses only on a drag, the ✕, or a timeout.
export function coachmarkArmed({ enabled, tabCount, dismissed }) {
  return !!enabled && !dismissed && (tabCount || 0) >= 2
}

// Read the persisted dismissal flag. A storage that throws (private mode,
// disabled) reports "already dismissed" so a browser that cannot remember the
// dismissal never nags every session.
export function coachmarkDismissed(storage) {
  try {
    return storage.getItem(HINT_KEY) === '1'
  } catch {
    return true
  }
}

// Insert the 'workspace' walkthrough step immediately after 'customize' (design
// §7.1), but only when the splits flag is on — teaching a gesture the flag-off
// build cannot perform would mislead. Returns a NEW array; the input is
// untouched. A missing 'customize' anchor leaves the steps unchanged.
export function insertWorkspaceStep(steps, enabled) {
  if (!enabled) return steps
  const at = steps.indexOf('customize')
  if (at < 0 || steps.includes('workspace')) return steps
  const out = steps.slice()
  out.splice(at + 1, 0, 'workspace')
  return out
}

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
