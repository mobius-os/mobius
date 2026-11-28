import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  HOLD_MS, chargeAfterFrame, decidePointerMove, isSwipeRight, swipeAllowed, movedBeyondSlop,
  holdComplete, runHoldCompletion,
} from './logoHoldMachine.js'

// How long a touch/pen pointerdown's provenance justifies suppressing a native
// contextmenu on the brand. The browser's long-press contextmenu fires at its own
// ~500ms threshold — comfortably inside this — so a genuine long-press is still
// suppressed; a keyboard contextmenu arriving later inherits no stale pointer type.
const POINTER_PROVENANCE_MS = 1500
// A touch/pen release within this distance of its press is a tap (a thumb tap
// routinely drifts past the 10px hold slop), and only if shorter than a
// deliberate hold would take to settle.
const TAP_SLOP_PX = 24
const TAP_MAX_MS = 700

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
  // Touch/pen taps are activated HERE, not by the browser's click: the brand
  // cancels its touchstart so Chrome never runs its own long-press gesture on
  // it. That gesture buzzes at ~500ms even with contextmenu prevented, which
  // stacked a second haptic on the hold's single completion pulse whenever the
  // finger stayed down. With no browser click, a touch tap calls onTap.
  onTap,
  // The live captured-scene descriptor or null. The hold hands its compression off
  // to this descriptor: while an animated mode change owns the logo, the mark holds
  // .84 and springs back at the scene's completion instead of
  // flashing its own ignite/snap (round 4 item 1). The gesture only READS it — to
  // know when the animated beat has settled so the hold's ownership latch can clear.
  transition = null,
}) {
  const [holding, setHolding] = useState(false)
  // '' | 'igniting' (spring into builder) | 'snapping' (snap back to single) — the
  // one-shot completion animation class for an INSTANT flip (reduced motion, an empty
  // tree). An ANIMATED beat suppresses it: the descriptor owns the spring instead.
  const [flourish, setFlourish] = useState('')
  // True once a completed HOLD started an animated scene, so the logo's compression
  // is handed to the descriptor's release rather than an immediate ignite/snap. It
  // persists across epoch SUPERSESSION (a keyboard/swipe retoggle during a hold-owned
  // beat inherits the compression against the newest epoch) and is cleared only when
  // no animated enter/exit descriptor remains — never merely because the epoch
  // changed. ShellBrand reads it to emit the is-beat-held classes.
  const [holdOwnsBeat, setHoldOwnsBeat] = useState(false)
  // { pressStamp, frameAt, charged, x, y, pointerId, pointerType } while a press is active; null between
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
  // The touch/pen press that may still resolve as a tap on release, kept apart
  // from pressRef so a thumb that drifts past the hold slop (abandoning the
  // HOLD) can still open the drawer, as the browser's own tap slop used to allow.
  const tapCandidateRef = useRef(null)

  const writeProgress = useCallback((p) => {
    const el = brandRef?.current
    if (el) el.style.setProperty('--hold-progress', String(p))
  }, [brandRef])

  const stopRaf = useCallback(() => {
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0 }
  }, [])

  // End the active press. suppressClick=true means the gesture ACTED — it flipped
  // the mode (a completed hold or a swipe) or the drawer opened underneath it — so
  // the trailing pointer click must NOT also toggle the drawer. false leaves the
  // native click alone.
  //
  // An ABANDONED press (movement, pointercancel, lost capture) passes false: the
  // hold is cancelled, but whether the gesture was still a tap is the BROWSER's
  // call, not ours. Its tap slop is device-calibrated and it simply does not
  // synthesize a click for a real drag or scroll, so suppressing here only ever
  // ate legitimate taps — a thumb tap on the brand routinely drifts past our 10px
  // hold-cancel slop, which made the drawer feel unopenable on a phone.
  // Releases the pointer capture taken at press start.
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
    tapCandidateRef.current = null
    const receipt = onToggleMode?.('hold')
    // A completed hold can be a deliberate no-op when Standard is the empty New
    // Chat landing, with no concrete screen to seed into Builder. Release the
    // gesture without playing feedback for a mode change that did not happen.
    if (receipt?.changed === false) {
      endPress({ suppressClick: true })
      return
    }
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
    const now = performance.now()
    press.charged = chargeAfterFrame(press.charged, now - press.frameAt)
    press.frameAt = now
    const p = press.charged / HOLD_MS
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
    // Primary button only: a mouse right-click or a pen barrel button is never a
    // tap or hold (touch always reports button 0).
    if (e.button !== 0) return
    if (pressRef.current) return // a press is already live — ignore a second pointer
    suppressClickRef.current = false
    const pressedAt = performance.now()
    pressRef.current = { pressStamp: e.timeStamp, frameAt: pressedAt, charged: 0, x: e.clientX, y: e.clientY, pointerId: e.pointerId, pointerType: e.pointerType }
    tapCandidateRef.current = e.pointerType === 'mouse'
      ? null
      : { pointerId: e.pointerId, x: e.clientX, y: e.clientY, stamp: e.timeStamp }
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
      tapCandidateRef.current = null
      onToggleMode?.('swipe')
      endPress({ suppressClick: true })
    } else if (decision === 'cancel') {
      // Movement beyond the hold slop — abandon the HOLD (no mode flip). Whether
      // it was still a tap is decided on release: a mouse by its native click, a
      // drifting thumb by the wider touch tap slop (releaseAsTap).
      endPress({ suppressClick: false })
    }
    // 'continue' → keep holding; the rAF loop keeps filling the ring.
  }, [onToggleMode, endPress])

  // Resolve a touch/pen release as a tap: within a thumb-sized slop and short of
  // a deliberate hold. Arms the click guard so a browser that still synthesizes a
  // click for a cancelled touchstart can never toggle the drawer twice.
  const releaseAsTap = useCallback((e, candidate) => {
    if (!candidate || candidate.pointerId !== e.pointerId) return
    const dx = e.clientX - candidate.x
    const dy = e.clientY - candidate.y
    if (Math.hypot(dx, dy) > TAP_SLOP_PX) return
    if (e.timeStamp - candidate.stamp >= TAP_MAX_MS) return
    suppressClickRef.current = true
    onTap?.()
  }, [onTap])

  const onPointerUp = useCallback((e) => {
    // Only this pointer's own release may consume its tap candidate; a second
    // finger lifting must not steal or clear the first finger's tap.
    const candidate = tapCandidateRef.current?.pointerId === e.pointerId
      ? tapCandidateRef.current
      : null
    if (candidate) tapCandidateRef.current = null
    const press = pressRef.current
    if (!press || e.pointerId !== press.pointerId) { releaseAsTap(e, candidate); return }
    // Classify the release by ELAPSED TIME + DISPLACEMENT, never by liveness
    // (review §4): a delayed final rAF must not let a ≥450ms release fall through
    // as a tap, and a fast flick whose only movement lands on this event is still
    // a swipe. Elapsed comes from the input timestamps, which record when the
    // finger really pressed and lifted: a busy device can deliver a quick tap's
    // release most of a second late, and that delay is not a hold.
    const dx = e.clientX - press.x
    const dy = e.clientY - press.y
    const elapsed = e.timeStamp - press.pressStamp
    // Swipe-right flips ONLY for touch/pen (finding F12); a mouse drag past the slop
    // hits the movedBeyondSlop cancel just below, so a mouse never toggles the mode.
    if (swipeAllowed(press.pointerType) && isSwipeRight(dx, dy)) { onToggleMode?.('swipe'); endPress({ suppressClick: true }); return }
    // Displacement past the hold slop only rules out the mode FLIP; a mouse's
    // native click and a touch tap's wider slop (releaseAsTap) still decide
    // tap-vs-drag.
    if (movedBeyondSlop(dx, dy)) { endPress({ suppressClick: false }); releaseAsTap(e, candidate); return }
    if (holdComplete(elapsed)) { completeHold(); return } // held long enough → flip
    // A genuine short tap: a mouse keeps its native click; touch/pen taps are
    // activated here because their touchstart was cancelled (see onTap).
    endPress({ suppressClick: false })
    releaseAsTap(e, candidate)
  }, [onToggleMode, endPress, completeHold, releaseAsTap])

  // The browser took the gesture (rare once touchstart is cancelled; a mouse or
  // pen can still be taken). Abandon the hold and any pending touch tap.
  const onPointerCancel = useCallback((e) => {
    const press = pressRef.current
    if (e && tapCandidateRef.current?.pointerId === e.pointerId) tapCandidateRef.current = null
    if (!press || (e && e.pointerId !== press.pointerId)) return
    endPress({ suppressClick: false })
  }, [endPress])

  // A LOST pointer capture ends the gesture's validity — cancel the live hold so a
  // dangling rAF can't complete a press the browser already took away (finding 11).
  const onLostPointerCapture = useCallback((e) => {
    const press = pressRef.current
    if (!press || (e && e.pointerId !== press.pointerId)) return
    // The browser took the pointer away: its release is no longer a tap.
    tapCandidateRef.current = null
    endPress({ suppressClick: false })
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
    if (!drawerOpen) return
    // A pending tap must not undo an open that came from elsewhere.
    tapCandidateRef.current = null
    if (pressRef.current) endPress({ suppressClick: true })
  }, [drawerOpen, endPress])

  // Cancel a live hold on any interruption that ends the gesture's validity: the
  // tab HIDING, the window BLURRING, or a bfcache PAGEHIDE (finding 11). The rAF
  // measures elapsed WALL time (performance.now), so a hide after pointerdown and a
  // return >450ms later would otherwise complete a hold the user never finished and
  // toggle the mode with no current gesture. lostpointercapture is handled by
  // onLostPointerCapture (wired on the brand button).
  useEffect(() => {
    const cancel = () => {
      tapCandidateRef.current = null
      if (pressRef.current) endPress({ suppressClick: true })
    }
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

  // Cancel the brand's touchstart (non-passive; React's touch listeners are
  // passive) so the browser runs no tap or long-press gesture on it: no
  // long-press haptic, menu or text selection. Pointer events still arrive, and
  // onPointerUp activates taps itself (see onTap). Single-finger only.
  useEffect(() => {
    const el = brandRef?.current
    if (!el || !enabled) return undefined
    const onTouchStart = (e) => { if (e.touches.length === 1 && e.cancelable) e.preventDefault() }
    el.addEventListener('touchstart', onTouchStart, { passive: false })
    return () => el.removeEventListener('touchstart', onTouchStart, { passive: false })
  }, [brandRef, enabled])

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
