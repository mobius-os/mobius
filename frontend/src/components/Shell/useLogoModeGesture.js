import { useCallback, useEffect, useRef, useState } from 'react'
import {
  HOLD_MS, decidePointerMove, isSwipeRight, movedBeyondSlop, holdComplete,
  runHoldCompletion,
} from './logoHoldMachine.js'

// Builder-mode activation, hosted on the TOP-LEFT logo cluster (owner placement):
// there is NO standalone toggle button — the Möbius mark itself is the control and
// the mode indicator (a 180deg twist + wordmark tint). The logo's single-tap job
// (open the drawer) is UNCHANGED — instant, no window, no timer on the tap path.
// Builder mode is entered/exited by a deliberate second gesture on the same mark:
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
}) {
  const [holding, setHolding] = useState(false)
  // '' | 'igniting' (spring into builder) | 'snapping' (snap back to single) — the
  // one-shot completion animation class, cleared on animationend.
  const [flourish, setFlourish] = useState('')
  // { t, x, y, pointerId } while a press is active; null between presses.
  const pressRef = useRef(null)
  const rafRef = useRef(0)
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
    runHoldCompletion({
      vibrate: vibrateFn,
      reducedMotion: prefersReducedMotion(),
      entering,
      // Restart the one-shot spring/snap (clear-then-set); cleared on animationend.
      startFlourish: (isEntering) => {
        setFlourish('')
        requestAnimationFrame(() => setFlourish(isEntering ? 'igniting' : 'snapping'))
      },
    })
    onToggleMode?.()
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
    if (e.pointerType === 'mouse' && e.button !== 0) return
    if (pressRef.current) return // a press is already live — ignore a second pointer
    suppressClickRef.current = false
    pressRef.current = { t: performance.now(), x: e.clientX, y: e.clientY, pointerId: e.pointerId }
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
    const decision = decidePointerMove(dx, dy)
    if (decision === 'swipe') {
      // Swipe-right flips the mode; slop suppresses the trailing tap.
      onToggleMode?.()
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
    if (isSwipeRight(dx, dy)) { onToggleMode?.(); endPress({ suppressClick: true }); return }
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

  const onContextMenu = useCallback((e) => {
    // Suppress the long-press context menu / selection callout ON THE BRAND during
    // a hold, so a press-and-hold activates builder mode instead of raising a menu.
    // Scoped to the brand only (this handler is on the brand button).
    if (pressRef.current) e.preventDefault()
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

  // Cancel a live rAF on unmount so a hold in flight can't tick a dead component.
  useEffect(() => () => { stopRaf() }, [stopRaf])

  return {
    holding, flourish,
    onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onContextMenu,
    consumeSuppressedClick, onAnimationEnd,
  }
}
