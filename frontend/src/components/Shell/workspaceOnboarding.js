// Onboarding + keyboard-undo gating for the workspace drag feature (design §7 /
// §3.5). Pure, dependency-free helpers so the arming/insertion/keymatch logic is
// unit-tested without a DOM, and Shell/WalkthroughOverlay stay declarative.

// localStorage key: the first-use coachmark shows at most once per browser.
export const HINT_KEY = 'mobius:hint-workspace'

// The coachmark arms when the workspace first holds ≥2 tabs, the splits flag is
// on, it has not already been dismissed this browser (design §7.2), AND the
// current presentation is the effective builder world with no immersive-solo up
// (`builderWorld`, M6). It teaches "drag tabs to split", so it may only appear
// where the tab strip actually exists — this branch removed the strip from single
// mode, so arming on tab count alone showed it for 12s where no tabs were onscreen,
// including over immersive content at z-120. It is deliberately NOT gated on a
// pointer event — an unrelated first tap must never kill it unread, so the caller
// dismisses only on a drag, the ✕, or a timeout.
export function coachmarkArmed({ enabled, tabCount, dismissed, builderWorld = true }) {
  return !!enabled && !dismissed && !!builderWorld && (tabCount || 0) >= 2
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

// The workspace gesture is taught by the first-use coachmark above, which arms
// at the moment the owner actually holds two tabs. A walkthrough step for it
// used to exist as well; it was retired with the shortened walkthrough rather
// than re-anchored, so there is one teaching path at the right moment instead
// of two at different ones.

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
