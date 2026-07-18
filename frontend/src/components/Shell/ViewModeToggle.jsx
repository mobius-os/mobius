import { useEffect, useState } from 'react'

// The single-screen <-> split-panes view-mode toggle (design: view-mode toggle).
// It lives in the shell top bar, in line with the Möbius logo cluster (the owner's
// "in line with the logo" placement — the brand is in .shell__bar, not the drawer).
// A compact icon button that reads as a toggle: shown in both modes, aria-pressed
// marks single active, quiet by default with a soft accent tint when single.
//
// Split-panes vs single-screen glyphs are authored inline rather than reusing
// @openai/apps-sdk-ui's Grid (Grid already means "Apps" in the drawer, so it would
// read ambiguously); they match the icon set's 24-viewBox rounded-rect outline.

/** Split-panes glyph (two side-by-side panes) for 'panes' view-mode. */
function PanesGlyph() {
  return (
    <svg
      width="18" height="18" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden="true"
    >
      <rect x="3" y="4.5" width="7.5" height="15" rx="2" />
      <rect x="13.5" y="4.5" width="7.5" height="15" rx="2" />
    </svg>
  )
}

/** Single-screen glyph (one pane) — the same outer bounds as PanesGlyph without
 *  the split, so the pair reads as one control's two states. */
function SingleGlyph() {
  return (
    <svg
      width="18" height="18" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden="true"
    >
      <rect x="3" y="4.5" width="18" height="15" rx="2" />
    </svg>
  )
}

/** The toggle button.
 *
 *  `vibrateRef` is an imperative shake handle: the workspace drag controller calls
 *  it (via the ref Shell threads through onDragBlocked) when a drag is attempted
 *  while dragging is disabled — single view-mode with a preserved multi-pane tree.
 *  The shake restarts by clearing the class for one frame first, so a rapid second
 *  attempt re-shakes rather than no-op'ing on an already-present class.
 *  prefers-reduced-motion swaps the transform shake for a non-motion outline pulse
 *  in CSS; both use the same `is-vibrating` class and both are finite animations,
 *  so onAnimationEnd is the clear. (If animations are disabled wholesale the class
 *  simply lingers invisibly and self-heals on the next trigger's clear-then-set.)
 *
 *  The vibrate fires during a drawer-row drag while the drawer is OPEN. That is
 *  perceivable here because the top bar (.shell__bar, z-index 100, opaque bg) paints
 *  ABOVE the drawer scrim (z 90) and panel (z 95, which starts below the bar), so no
 *  extra z-index raise or drawer-edge pulse is needed — the bar toggle is never
 *  covered. `inert` on the bar (while the drawer is open) blocks interaction only,
 *  not painting or animation. */
export default function ViewModeToggle({ viewMode, onToggle, vibrateRef }) {
  const single = viewMode === 'single'
  const [vibrating, setVibrating] = useState(false)
  useEffect(() => {
    if (!vibrateRef) return undefined
    vibrateRef.current = () => {
      setVibrating(false)
      requestAnimationFrame(() => setVibrating(true))
    }
    return () => { if (vibrateRef.current) vibrateRef.current = null }
  }, [vibrateRef])
  return (
    <button
      type="button"
      className={`shell__viewmode${vibrating ? ' is-vibrating' : ''}`}
      aria-pressed={single}
      aria-label={single ? 'Single screen' : 'Split panes'}
      title={single ? 'Single screen' : 'Split panes'}
      // A pure state flip — it only dispatches the toggle. It never opens or closes
      // the drawer (design: view-mode toggle); it is a sibling of the brand/drawer
      // button in the bar, so its click never reaches the drawer control.
      onClick={onToggle}
      onAnimationEnd={() => setVibrating(false)}
    >
      {single ? <SingleGlyph /> : <PanesGlyph />}
    </button>
  )
}
