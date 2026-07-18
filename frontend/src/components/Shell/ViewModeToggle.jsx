import { useEffect, useRef, useState } from 'react'

// The single-screen <-> builder-mode toggle (design: builder-mode activation).
// It lives in the shell top bar, in line with the Möbius logo cluster (the owner's
// "in line with the logo" placement — the brand is in .shell__bar, not the drawer).
// A compact icon button that reads as a toggle: shown in both modes, aria-pressed
// marks single active, quiet by default with a soft accent tint when single.
//
// Activation (design):
//   - single tap/click flips the mode INSTANTLY — no 250-300ms double-tap delay is
//     ever added to the single tap;
//   - a SECOND activation inside the double-click window is swallowed (no
//     toggle-back) and idempotently ENTERS builder mode, replaying the morph;
//   - a touch swipe-right on the button (touch pointers only, dx >= 28px and
//     horizontal-dominant) also idempotently enters builder mode.
// touch-action:pan-y pinch-zoom (in CSS) suppresses the double-tap zoom on the
// target while leaving vertical scroll + pinch zoom intact — viewport zoom is
// never disabled.

// The double-activation window. It does NOT delay the first tap — that acts
// immediately; this only reclassifies a fast follow-up as the second of a double.
const DOUBLE_MS = 300
// Minimum horizontal travel for a swipe-right, with clear horizontal dominance.
const SWIPE_DX = 28

// ONE SVG outer frame that MORPHS: a center divider whose opacity/scale animates
// between "split panes" (builder — divider present) and "single screen" (divider
// faded). This genuinely morphs, unlike swapping two whole SVGs. The morph is
// CSS-driven off the button's aria-pressed state (+ a one-shot flourish class).
function ViewGlyph() {
  return (
    <svg
      className="shell__viewmode-glyph"
      width="18" height="18" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinejoin="round" aria-hidden="true"
    >
      <rect x="3" y="4.5" width="18" height="15" rx="2" />
      <line
        className="shell__viewmode-divider"
        x1="12" y1="4.5" x2="12" y2="19.5" strokeLinecap="round"
      />
    </svg>
  )
}

/** The toggle button.
 *
 *  `onToggle` flips the mode (single tap). `onEnterBuilder` idempotently enters
 *  builder mode (the double gesture + the touch swipe) — the shell makes it a
 *  no-op when already in builder, so it is safe to fire either way.
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
export default function ViewModeToggle({ viewMode, onToggle, onEnterBuilder, vibrateRef }) {
  const single = viewMode === 'single'
  const [vibrating, setVibrating] = useState(false)
  const [morphing, setMorphing] = useState(false)
  // Timestamp of the last activation, for the no-delay double detection.
  const lastActivateRef = useRef(0)
  // Live touch-swipe tracking; null unless a touch pointer is down on the button.
  const swipeRef = useRef(null)
  // Set true when a swipe consumed the gesture, so the synthetic click that may
  // follow a touch swipe does not ALSO toggle.
  const swipeConsumedRef = useRef(false)

  useEffect(() => {
    if (!vibrateRef) return undefined
    vibrateRef.current = () => {
      setVibrating(false)
      requestAnimationFrame(() => setVibrating(true))
    }
    return () => { if (vibrateRef.current) vibrateRef.current = null }
  }, [vibrateRef])

  // Restart the one-shot morph flourish (clear-then-set, like the vibrate) so a
  // repeated double/swipe re-plays it even when the mode does not change.
  const playMorph = () => {
    setMorphing(false)
    requestAnimationFrame(() => setMorphing(true))
  }

  const enterBuilder = () => {
    onEnterBuilder?.()
    playMorph()
  }

  const handleClick = () => {
    // Absorb the synthetic click that can trail a completed touch swipe.
    if (swipeConsumedRef.current) { swipeConsumedRef.current = false; return }
    const now = Date.now()
    const isDouble = now - lastActivateRef.current < DOUBLE_MS
    lastActivateRef.current = now
    // The first tap already acted (below); a second inside the window is swallowed
    // and resolves the whole double gesture to "enter builder", idempotently.
    if (isDouble) enterBuilder()
    else onToggle?.()
  }

  const onPointerDown = (e) => {
    swipeConsumedRef.current = false
    swipeRef.current = e.pointerType === 'touch'
      ? { x: e.clientX, y: e.clientY }
      : null
  }
  const onPointerMove = (e) => {
    const start = swipeRef.current
    if (!start) return
    const dx = e.clientX - start.x
    const dy = e.clientY - start.y
    // Swipe-right, clearly horizontal — enter builder once per gesture.
    if (dx >= SWIPE_DX && Math.abs(dx) > Math.abs(dy)) {
      swipeRef.current = null
      swipeConsumedRef.current = true
      enterBuilder()
    }
  }
  const endSwipe = () => { swipeRef.current = null }

  const cls = `shell__viewmode${vibrating ? ' is-vibrating' : ''}${morphing ? ' is-morphing' : ''}`
  return (
    <button
      type="button"
      className={cls}
      aria-pressed={single}
      aria-label={single ? 'Single screen' : 'Builder mode'}
      title={single ? 'Single screen' : 'Builder mode'}
      // A pure state flip / builder-enter — it never opens or closes the drawer
      // (design: view-mode toggle); it is a sibling of the brand/drawer button in
      // the bar, so its interactions never reach the drawer control.
      onClick={handleClick}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endSwipe}
      onPointerCancel={endSwipe}
      onAnimationEnd={() => { setVibrating(false); setMorphing(false) }}
    >
      <ViewGlyph />
    </button>
  )
}
