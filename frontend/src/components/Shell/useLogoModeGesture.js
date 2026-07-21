import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  HOLD_MS, decidePointerMove, isSwipeRight, swipeAllowed, movedBeyondSlop, holdComplete,
  runHoldCompletion,
} from './logoHoldMachine.js'

// How long a touch/pen pointerdown's provenance justifies suppressing a native
// contextmenu on the brand. The browser's long-press contextmenu fires at its own
// ~500ms threshold — comfortably inside this — so a genuine long-press is still
// suppressed; a keyboard contextmenu arriving later inherits no stale pointer type.
const POINTER_PROVENANCE_MS = 1500

// Builder-mode shortcuts hosted on the top-left logo cluster. The explicit header
// button is the discoverable one-tap path; the mark remains a mode indicator (180deg
// twist + wordmark tint), and its single-tap drawer job is unchanged. Power users can
// also enter/exit builder mode with a deliberate second gesture on the mark:
//
//   - HOLD ~450ms (touch OR mouse press-and-hold): the CHARGE model — the logo
//     itself COMPRESSES (scale driven by the --hold-progress rAF var), and on
//     completion the mode flips with a SINGLE haptic pulse + the logo springs
//     (enter) or snaps (exit) back. There are no mid-hold ramp ticks: one clean
//     vibration at completion, never a buzzy series (owner call 2026-07-19). An
//     early release is just a tap → drawer; movement beyond a small slop cancels
//     cleanly.
//   - a touch SWIPE-RIGHT flips the mode too; its slop suppresses the trailing
//     click so a swipe never also toggles the drawer.
//
// There are NO double-tap semantics — two taps are just two taps.
//
// The pure thresholds/predicates and the completion feedback live in
// logoHoldMachine.js so the contract is unit-testable without React/DOM.
//
// The press is POINTER-CAPTURED and keyed by pointerId (adversarial review §5/§6):
// events for a press that leaves the brand, or a second finger, cannot mis-drive
// the state machine, and any drawer-open from another path cancels a live hold.

export function prefersReducedMotion() {
  try {
    return typeof window !== 'undefined' && !!window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch { return false }
}

export function useLogoModeGesture({
  onToggleMode, brandRef, enabled = true, drawerOpen = false, builderModeActive = false,
  // The live mode descriptor (modeMachine transition) or null. The hold hands its
  // compression off to this descriptor: while an animated enter/exit beat owns the
  // logo, the mark holds .84 and springs back at the beat's completion instead of
  // flashing its own ignite/snap (round 4 item 1). The gesture only READS it — to
  // know when the animated beat has settled so the hold's ownership latch can clear.
  transition = null,
}) {
  const [holding, setHolding] = useState(false)
  // '' | 'igniting' (spring into builder) | 'snapping' (snap back to single) — the
  // one-shot completion animation class for an INSTANT flip (reduced motion, an empty
  // tree). An ANIMATED beat suppresses it: the descriptor owns the spring instead.
  const [flourish, setFlourish] = useState('')
  // True once a completed HOLD started an animated beat, so the logo's compression is
  // handed to the descriptor's release rather than an immediate ignite/snap. It
  // persists across epoch SUPERSESSION (a keyboard/swipe retoggle during a hold-owned
  // beat inherits the compression against the newest epoch) and is cleared only when
  // no animated enter/exit descriptor remains — never merely because the epoch
  // changed. ShellBrand reads it to emit the is-beat-held classes.
  const [holdOwnsBeat, setHoldOwnsBeat] = useState(false)
  // { t, x, y, pointerId, pointerType } while a press is active; null between
  // presses. pointerType is retained so onContextMenu can suppress the native
  // long-press menu for touch/pen even after the press ended (see below).
  const pressRef = useRef(null)
  const rafRef = useRef(0)
  // The pointerType of the most recent pointerdown on the brand ('touch' | 'pen' |
  // 'mouse' | ''). onContextMenu reads it to decide whether a contextmenu is a
  // touch/pen long-press (always suppress on this control) or a desktop mouse
  // right-click (keep its native menu). Persists past press-end so a late-firing
  // long-press contextmenu is still recognized as touch. See onContextMenu.
  const lastPointerTypeRef = useRef('')
  // When that provenance was stamped. It EXPIRES (finding 5): a touch/pen menu is
  // suppressed only within a short window of the pointer that set it — otherwise a
  // KEYBOARD context menu (Menu key / Shift+F10) on the focused brand, which has no
  // pointer event, would inherit a stale 'touch'/'pen' and be wrongly suppressed,
  // regressing keyboard/AT access to the native menu. A keydown on the brand also
  // clears it (onKeyDown below) so the keyboard path never inherits pointer state.
  const lastPointerTypeAtRef = useRef(0)
  // Set when a POINTER gesture (completed hold, swipe, or drag) consumed the
  // activation, so the trailing compatibility click does NOT also toggle the
  // drawer. Only ever read for a pointer click (detail >= 1) — a keyboard click
  // (detail 0) is never the compat click, so a stale flag can't eat the next Enter
  // (review §13).
  const suppressClickRef = useRef(false)

  const writeProgress = useCallback((p) => {
    const el = brandRef?.current
    if (el) el.style.setProperty('--hold-progress', String(p))
  }, [brandRef])

  const stopRaf = useCallback(() => {
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
  }, [])

  // End the active press. suppressClick=true means the gesture consumed the
  // activation (a completed hold, a swipe, or a drag), so the trailing pointer
  // click must NOT also toggle the drawer; false means it was a plain TAP → let
  // the native click open the drawer, UNCHANGED and with zero latency. Releases
  // the pointer capture taken at press start.
  const endPress = useCallback(({ suppressClick }) => {
    stopRaf()
    writeProgress(0)
    const press = pressRef.current
    pressRef.current = null
    setHolding(false)
    if (press && brandRef?.current) {
      try { brandRef.current.releasePointerCapture?.(press.pointerId) } catch { /* released */ }
    }
    if (suppressClick) suppressClickRef.current = true
  }, [stopRaf, writeProgress, brandRef])

  // Stable feature-detected haptic — a graceful no-op where the Vibration API is
  // absent (iOS Safari). Stable identity so it doesn't churn the rAF callbacks.
  const vibrateFn = useCallback((ms) => {
    if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') {
      try { navigator.vibrate(ms) } catch { /* unsupported */ }
    }
  }, [])

  const completeHold = useCallback(() => {
    if (!pressRef.current) return
    writeProgress(1)
    // Direction is decided by the CURRENT mode: entering builder springs + buzzes
    // 12; snapping back to single snaps + buzzes 8. The card-deal / pane-out are
    // CSS driven by the mode class change, not here.
    const entering = !builderModeActive
    // Thread the HONEST cause (finding F13): a completed hold is 'hold'. Toggle FIRST
    // and inspect the receipt so the flourish decision knows whether an animated beat
    // armed. The mode flip is synchronous, so the descriptor class takes over the
    // logo's scale in the SAME batched paint (endPress resets --hold-progress to 0,
    // but the is-beat-held animation's backwards fill holds .84 through the delay).
    const receipt = onToggleMode?.('hold')
    const animated = !!(receipt && receipt.animated)
    if (animated) setHoldOwnsBeat(true)
    runHoldCompletion({
      vibrate: vibrateFn,
      reducedMotion: prefersReducedMotion(),
      entering,
      // An INSTANT flip (empty tree / reduced motion) keeps the immediate ignite/snap;
      // an ANIMATED beat hands the spring to the descriptor, so no flourish here (the
      // haptic always fires — motion is what the beat owns, feedback is not).
      startFlourish: animated ? undefined : (isEntering) => {
        setFlourish('')
        requestAnimationFrame(() => setFlourish(isEntering ? 'igniting' : 'snapping'))
      },
    })
    endPress({ suppressClick: true })
  }, [writeProgress, onToggleMode, endPress, builderModeActive, vibrateFn])

  // The rAF loop drives the compress + completion — no setTimeout anywhere, so the
  // tap path carries zero latency and the hold cannot leak a timer. It fires NO
  // haptic itself: the ONLY vibration is the single completion pulse in
  // completeHold, so a hold buzzes exactly once (owner call 2026-07-19 — the old
  // mid-hold ramp ticks read as a double/triple buzz within the ~450ms window).
  const tick = useCallback(() => {
    const press = pressRef.current
    if (!press) return
    const p = (performance.now() - press.t) / HOLD_MS
    if (p >= 1) { completeHold(); return }
    writeProgress(p)
    rafRef.current = requestAnimationFrame(tick)
  }, [completeHold, writeProgress])

  const onPointerDown = useCallback((e) => {
    if (!enabled) return
    // Record the pointer type + when, before the mouse-button guard below can bail
    // — onContextMenu reads it to suppress the touch/pen long-press menu even for a
    // right-click-style contextmenu (review: item C), but only while FRESH (finding 5).
    lastPointerTypeRef.current = e.pointerType
    lastPointerTypeAtRef.current = performance.now()
    if (e.pointerType === 'mouse' && e.button !== 0) return
    if (pressRef.current) return // a press is already live — ignore a second pointer
    suppressClickRef.current = false
    pressRef.current = { t: performance.now(), x: e.clientX, y: e.clientY, pointerId: e.pointerId, pointerType: e.pointerType }
    setHolding(true)
    writeProgress(0)
    stopRaf()
    // Capture so a press that leaves the brand still delivers move/up here and the
    // machine ends deterministically (review §5).
    try { brandRef?.current?.setPointerCapture?.(e.pointerId) } catch { /* capture optional */ }
    rafRef.current = requestAnimationFrame(tick)
  }, [enabled, tick, stopRaf, writeProgress, brandRef])

  const onPointerMove = useCallback((e) => {
    const press = pressRef.current
    if (!press || e.pointerId !== press.pointerId) return
    const dx = e.clientX - press.x
    const dy = e.clientY - press.y
    // pointerType gates the swipe (finding F12): a mouse drag classifies as 'cancel'.
    const decision = decidePointerMove(dx, dy, press.pointerType)
    if (decision === 'swipe') {
      // Swipe-right flips the mode; slop suppresses the trailing tap. Honest cause.
      onToggleMode?.('swipe')
      endPress({ suppressClick: true })
    } else if (decision === 'cancel') {
      // Became a scroll/drag — abandon the hold cleanly (no drawer either).
      endPress({ suppressClick: true })
    }
    // 'continue' → keep holding; the rAF loop keeps filling the ring.
  }, [onToggleMode, endPress])

  const onPointerUp = useCallback((e) => {
    const press = pressRef.current
    if (!press || e.pointerId !== press.pointerId) return
    // Classify the release by ELAPSED TIME + DISPLACEMENT, never by liveness
    // (review §4): a delayed final rAF must not let a ≥450ms release fall through
    // as a tap, and a fast flick whose only movement lands on this event is still
    // a swipe.
    const dx = e.clientX - press.x
    const dy = e.clientY - press.y
    const elapsed = performance.now() - press.t
    // Swipe-right flips ONLY for touch/pen (finding F12); a mouse drag past the slop
    // hits the movedBeyondSlop cancel just below, so a mouse never toggles the mode.
    if (swipeAllowed(press.pointerType) && isSwipeRight(dx, dy)) { onToggleMode?.('swipe'); endPress({ suppressClick: true }); return }
    if (movedBeyondSlop(dx, dy)) { endPress({ suppressClick: true }); return } // a drag → cancel
    if (holdComplete(elapsed)) { completeHold(); return } // held long enough → flip
    // A genuine short tap → let the native click open the drawer, unchanged.
    endPress({ suppressClick: false })
  }, [onToggleMode, endPress, completeHold])

  const onPointerCancel = useCallback((e) => {
    const press = pressRef.current
    if (!press || (e && e.pointerId !== press.pointerId)) return
    endPress({ suppressClick: true })
  }, [endPress])

  // A LOST pointer capture ends the gesture's validity — cancel the live hold so a
  // dangling rAF can't complete a press the browser already took away (finding 11).
  const onLostPointerCapture = useCallback((e) => {
    const press = pressRef.current
    if (!press || (e && e.pointerId !== press.pointerId)) return
    endPress({ suppressClick: true })
  }, [endPress])

  const onContextMenu = useCallback((e) => {
    // Suppress the native long-press context menu / image-callout ON THE BRAND for
    // touch and pen. The brand is a control, never a menu target, so a touch/pen
    // long-press must ALWAYS activate builder mode instead of raising a menu.
    //
    // Gating on a LIVE press alone (pressRef.current) LEAKS the menu: the browser's
    // long-press `contextmenu` fires at its OWN threshold (~500ms), which can land
    // AFTER the ~450ms hold completes — completeHold has already run endPress and
    // nulled pressRef — or after a slop-cancel nulled it, so the guard sees no press
    // and lets the native menu through. That timing race is the owner's "sometimes
    // holding the logo opens the image [with a download option]" report. Keying off
    // the LAST pointer type (set on every pointerdown, right-click included)
    // suppresses touch/pen while leaving a desktop MOUSE right-click its native
    // menu. The touch/pen provenance EXPIRES (finding 5): it justifies suppression
    // only within POINTER_PROVENANCE_MS of the pointerdown that set it, so a later
    // KEYBOARD contextmenu (Menu key / Shift+F10, no pointer event) on the focused
    // brand is NOT suppressed and keyboard/AT users still reach the native menu. A
    // still-live press is always suppressed (its long-press menu is definitionally
    // fresh). INVARIANT: contextmenu on the brand is prevented for every FRESH
    // touch/pen interaction, whether or not a press is still live.
    const pt = lastPointerTypeRef.current
    const fresh = (performance.now() - lastPointerTypeAtRef.current) < POINTER_PROVENANCE_MS
    if (((pt === 'touch' || pt === 'pen') && fresh) || pressRef.current) e.preventDefault()
  }, [])

  // A keyboard interaction on the brand clears pointer provenance (finding 5): the
  // very next contextmenu is then keyboard-invoked and must reach the native menu.
  // Wired into the brand's onKeyDown alongside the mode-toggle key handling.
  const onKeyDown = useCallback(() => {
    lastPointerTypeRef.current = ''
    lastPointerTypeAtRef.current = 0
  }, [])

  // Read + reset the pointer-click suppression. `detail` is the click event's
  // detail: 0 means a keyboard-generated click, which is NEVER the compat click a
  // pointer gesture leaves behind, so it is never suppressed (review §13). The
  // caller opens the drawer only when this returns false.
  const consumeSuppressedClick = useCallback((detail) => {
    if (detail === 0) return false
    if (!suppressClickRef.current) return false
    suppressClickRef.current = false
    return true
  }, [])

  // Clears the one-shot spring/snap class when its animation ends.
  const onAnimationEnd = useCallback(() => { setFlourish('') }, [])

  // A drawer-open from ANY path (plain Enter, a queued open, a sibling) while a
  // hold is live cancels it — otherwise the rAF keeps ticking and later flips the
  // mode behind the open drawer (review §6). suppressClick so the eventual pointer
  // release can't close the drawer the user just opened.
  useEffect(() => {
    if (drawerOpen && pressRef.current) endPress({ suppressClick: true })
  }, [drawerOpen, endPress])

  // Cancel a live hold on any interruption that ends the gesture's validity: the
  // tab HIDING, the window BLURRING, or a bfcache PAGEHIDE (finding 11). The rAF
  // measures elapsed WALL time (performance.now), so a hide after pointerdown and a
  // return >450ms later would otherwise complete a hold the user never finished and
  // toggle the mode with no current gesture. lostpointercapture is handled by
  // onLostPointerCapture (wired on the brand button).
  useEffect(() => {
    const cancel = () => { if (pressRef.current) endPress({ suppressClick: true }) }
    const onHidden = () => { if (typeof document !== 'undefined' && document.visibilityState === 'hidden') cancel() }
    window.addEventListener('blur', cancel)
    window.addEventListener('pagehide', cancel)
    document.addEventListener('visibilitychange', onHidden)
    return () => {
      window.removeEventListener('blur', cancel)
      window.removeEventListener('pagehide', cancel)
      document.removeEventListener('visibilitychange', onHidden)
    }
  }, [endPress])

  // Cancel a live rAF on unmount so a hold in flight can't tick a dead component.
  useEffect(() => () => { stopRaf() }, [stopRaf])

  // Clear the hold-owns-beat latch the moment no animated enter/exit descriptor
  // remains — the beat completed, was cancelled, or degraded to a drag preview. Keyed
  // on the descriptor's PHASE, never its epoch: a keyboard/swipe supersession keeps an
  // animated beat live under a new id, so the compression rides through to the newest
  // beat rather than clearing on the id change (round 4 item 1 interaction decisions).
  useLayoutEffect(() => {
    const animatedBeat = !!transition
      && (transition.phase === 'entering' || transition.phase === 'exiting')
    if (!animatedBeat && holdOwnsBeat) setHoldOwnsBeat(false)
  }, [transition, holdOwnsBeat])

  return {
    holding, flourish, holdOwnsBeat,
    onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onContextMenu,
    onKeyDown, onLostPointerCapture,
    consumeSuppressedClick, onAnimationEnd,
  }
}
