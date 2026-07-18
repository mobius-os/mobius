// The logo HOLD/SWIPE activation, as PURE, DOM-free, timer-free predicates so the
// whole contract is unit-testable (owner activation semantics).
//
// The brand/logo keeps its instant single-tap drawer toggle UNCHANGED — no window,
// no timer on the tap path. Builder mode is entered/exited by a deliberate second
// gesture on the same control:
//   - HOLD the mark ~450ms (touch OR mouse press-and-hold): a ring fills; on
//     completion the view mode flips. An early release is just a tap (→ drawer);
//     movement beyond a small slop cancels the hold cleanly.
//   - a touch SWIPE-RIGHT flips the mode too, with the accent trace following the
//     finger. Its slop suppresses the trailing click so a swipe never also toggles
//     the drawer.
// There are NO double-tap semantics — two taps are just two taps (drawer open then
// close), which is native and honest.
export const HOLD_MS = 450
// Minimum horizontal travel for a swipe-right, with clear horizontal dominance.
export const SWIPE_DX = 28
// Movement past this (px) during a press cancels the hold — it became a scroll or
// drag, not a hold. Kept below SWIPE_DX so a real swipe is classified as a swipe
// (decidePointerMove checks the swipe first), not merely a cancel.
export const MOVE_CANCEL_PX = 10

// The hold has been held long enough to activate.
export function holdComplete(elapsedMs) {
  return elapsedMs >= HOLD_MS
}

// A press released before the threshold is a plain TAP (the drawer), not a flip.
export function releasedAsTap(elapsedMs) {
  return elapsedMs < HOLD_MS
}

// A deliberate horizontal swipe-right.
export function isSwipeRight(dx, dy) {
  return dx >= SWIPE_DX && Math.abs(dx) > Math.abs(dy)
}

// Movement large enough to abandon a hold (when it is not a qualifying swipe).
export function movedBeyondSlop(dx, dy) {
  return Math.hypot(dx, dy) > MOVE_CANCEL_PX
}

// What a pointermove during a press means. Swipe wins over cancel, so a fast
// horizontal drag flips the mode rather than merely aborting the hold.
export function decidePointerMove(dx, dy) {
  if (isSwipeRight(dx, dy)) return 'swipe'
  if (movedBeyondSlop(dx, dy)) return 'cancel'
  return 'continue'
}

// A single short haptic pulse on completion.
export const HOLD_HAPTIC_MS = 12

// Completion feedback for an entered/exited hold, with injected deps so it is DOM-
// and React-free (and unit-testable). Fired EXACTLY once per completed hold — the
// caller invokes it only from the completion path, never on a cancel.
//   - HAPTIC always fires where the Vibration API exists (a single short pulse).
//     iOS Safari has NO web Vibration API, so `vibrate` is simply absent there and
//     this is a graceful no-op — feature-detected, no polyfill, no fake fallback.
//   - the outward accent PULSE is motion, so it is SKIPPED under reduced motion;
//     the haptic is NOT skipped. The persistent state (twisted logo + tinted
//     wordmark) and the room flourish are the durable confirmation, so there is
//     deliberately NO toast or text label.
export function runHoldCompletion({ vibrate, reducedMotion, startPulse }) {
  if (typeof vibrate === 'function') vibrate(HOLD_HAPTIC_MS)
  if (!reducedMotion && typeof startPulse === 'function') startPulse()
}
