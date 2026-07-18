import { useCallback, useEffect, useRef, useState } from 'react'
import { HOLD_MS, decidePointerMove, runHoldCompletion } from './logoHoldMachine.js'

// Builder-mode activation, hosted on the TOP-LEFT logo cluster (owner placement):
// there is NO standalone toggle button — the Möbius mark itself is the control and
// the mode indicator (a 180deg twist + wordmark tint). The logo's single-tap job
// (open the drawer) is UNCHANGED — instant, no window, no timer on the tap path.
// Builder mode is entered/exited by a deliberate second gesture on the same mark:
//
//   - HOLD ~450ms (touch OR mouse press-and-hold): a ring fills around the mark
//     (a --hold-progress CSS var driven by a rAF loop → conic-gradient); on
//     completion the mode flips with a haptic pulse + an outward accent pulse.
//     An early release is just a tap → drawer; movement beyond a small slop
//     cancels cleanly.
//   - a touch SWIPE-RIGHT flips the mode too; its slop suppresses the trailing
//     click so a swipe never also toggles the drawer.
//
// There are NO double-tap semantics — two taps are just two taps.
//
// The pure thresholds/predicates and the completion feedback live in
// logoHoldMachine.js so the contract is unit-testable without React/DOM.

export function prefersReducedMotion() {
  try {
    return typeof window !== 'undefined' && !!window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch { return false }
}

export function useLogoModeGesture({ onToggleMode, vibrateRef, brandRef, enabled = true }) {
  const [vibrating, setVibrating] = useState(false)
  const [holding, setHolding] = useState(false)
  const [pulsing, setPulsing] = useState(false)
  // { t, x, y } while a press is active; null between presses.
  const pressRef = useRef(null)
  const rafRef = useRef(0)
  // Set when a gesture (completed hold, swipe, or drag) consumed the activation,
  // so the trailing click does NOT also toggle the drawer.
  const suppressClickRef = useRef(false)

  useEffect(() => {
    if (!vibrateRef) return undefined
    // The vibrate-deny (a drag attempted in single mode with a parked multi-pane
    // tree) shakes the LOGO now. It stays perceivable during a drawer-open drag
    // because .shell__bar (z-index 100, opaque) paints above the drawer scrim.
    vibrateRef.current = () => {
      setVibrating(false)
      requestAnimationFrame(() => setVibrating(true))
    }
    return () => { if (vibrateRef.current) vibrateRef.current = null }
  }, [vibrateRef])

  const writeProgress = useCallback((p) => {
    const el = brandRef?.current
    if (el) el.style.setProperty('--hold-progress', String(p))
  }, [brandRef])

  const stopRaf = useCallback(() => {
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
  }, [])

  // End the active press. suppressClick=true means the gesture consumed the
  // activation (a completed hold, a swipe, or a drag), so the trailing click must
  // NOT also toggle the drawer; false means it was a plain TAP → let the native
  // click open the drawer, UNCHANGED and with zero latency.
  const endPress = useCallback(({ suppressClick }) => {
    stopRaf()
    writeProgress(0)
    pressRef.current = null
    setHolding(false)
    if (suppressClick) suppressClickRef.current = true
  }, [stopRaf, writeProgress])

  const completeHold = useCallback(() => {
    if (!pressRef.current) return
    writeProgress(1)
    runHoldCompletion({
      vibrate: (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function')
        ? (ms) => navigator.vibrate(ms)
        : undefined,
      reducedMotion: prefersReducedMotion(),
      // Restart the outward accent pulse (clear-then-set) so a repeated hold
      // re-plays it; the class is cleared on its animationend.
      startPulse: () => { setPulsing(false); requestAnimationFrame(() => setPulsing(true)) },
    })
    onToggleMode?.()
    endPress({ suppressClick: true })
  }, [writeProgress, onToggleMode, endPress])

  // The rAF loop drives BOTH the ring fill and completion — no setTimeout anywhere,
  // so the tap path carries zero latency and the hold cannot leak a timer.
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
    suppressClickRef.current = false
    pressRef.current = { t: performance.now(), x: e.clientX, y: e.clientY }
    setHolding(true)
    writeProgress(0)
    stopRaf()
    rafRef.current = requestAnimationFrame(tick)
  }, [enabled, tick, stopRaf, writeProgress])

  const onPointerMove = useCallback((e) => {
    const press = pressRef.current
    if (!press) return
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

  const onPointerUp = useCallback(() => {
    // Nothing active means the hold already completed/cancelled (click already
    // suppressed). Otherwise this is a release BEFORE completion — a plain TAP, so
    // do NOT suppress: the native click opens the drawer, exactly as before.
    if (!pressRef.current) return
    endPress({ suppressClick: false })
  }, [endPress])

  const onPointerCancel = useCallback(() => {
    if (!pressRef.current) return
    endPress({ suppressClick: true })
  }, [endPress])

  const onContextMenu = useCallback((e) => {
    // Suppress the long-press context menu / selection callout ON THE BRAND during
    // a hold, so a press-and-hold activates builder mode instead of raising a menu.
    // Scoped to the brand only (this handler is on the brand button).
    if (pressRef.current) e.preventDefault()
  }, [])

  // Read + reset the "this click was consumed by the gesture" flag; the caller
  // opens the drawer only when this returns false.
  const consumeSuppressedClick = useCallback(() => {
    if (!suppressClickRef.current) return false
    suppressClickRef.current = false
    return true
  }, [])

  const onAnimationEnd = useCallback(() => {
    setVibrating(false)
    setPulsing(false)
  }, [])

  // Cancel a live rAF on unmount so a hold in flight can't tick a dead component.
  useEffect(() => () => { stopRaf() }, [stopRaf])

  return {
    vibrating, holding, pulsing,
    onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onContextMenu,
    consumeSuppressedClick, onAnimationEnd,
  }
}
