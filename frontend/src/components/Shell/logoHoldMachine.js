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

// SINGLE-PULSE haptics (owner call 2026-07-19): a completed hold fires EXACTLY
// ONE vibration, at completion. The earlier CHARGE model also buzzed two light
// ramp ticks at 50% and 85% of the hold, but within a ~450ms window three pulses
// land on top of each other and read as a buzzy double/triple tap instead of one
// clean confirmation (owner: "feels like two vibrations instead of one"). The
// ramp ticks are gone; the completion pulse is the only haptic — 12ms entering
// builder, 8ms snapping back to single. Those lengths stay as-is: a lone 12/8ms
// pulse already reads as one crisp tap, so there is nothing left to soften once
// the ramp is removed. INVARIANT: navigator.vibrate is called at most once per
// completed hold (from runHoldCompletion), and never on a cancel/tap.
export const HOLD_HAPTIC_ENTER_MS = 12
export const HOLD_HAPTIC_EXIT_MS = 8

// Completion feedback for a charged hold, with injected deps so it is DOM- and
// React-free (and unit-testable). Fired EXACTLY once per completed hold — the
// caller invokes it only from the completion path, never on a cancel.
//   - HAPTIC always fires where the Vibration API exists (12 entering builder, 8
//     exiting). iOS Safari has NO web Vibration API, so `vibrate` is simply absent
//     and this is a graceful no-op — feature-detected, no polyfill.
//   - the logo SPRING/SNAP is motion, SKIPPED under reduced motion (the haptic is
//     not). The persistent state (180° twisted logo + tinted wordmark + living
//     halo) plus the card-deal/pane-out are the durable confirmation, so there is
//     deliberately NO toast or text label.
export function runHoldCompletion({ vibrate, reducedMotion, entering, startFlourish }) {
  if (typeof vibrate === 'function') vibrate(entering ? HOLD_HAPTIC_ENTER_MS : HOLD_HAPTIC_EXIT_MS)
  if (!reducedMotion && typeof startFlourish === 'function') startFlourish(entering)
}

// The living halo's drift, as a PURE function of time so its motion is unit-
// testable and allocation-free at the call site (the rAF loop reuses one object).
// Two summed sines at IRRATIONAL frequency ratios never repeat, so the glow never
// looks looped. Returns normalized drift the CSS composes: scale ~[0.94,1.06],
// offset in px, and an opacity multiplier ~[0.7,1].
const HALO_F1 = 0.00037 // rad/ms  (~2.7s)
const HALO_F2 = 0.00061 * Math.SQRT2 // irrational ratio to F1
export function haloFrame(tMs, out = {}) {
  const a = Math.sin(tMs * HALO_F1)
  const b = Math.sin(tMs * HALO_F2 + 1.7)
  out.scale = 1 + 0.06 * (0.6 * a + 0.4 * b)
  out.x = 3.2 * b
  out.y = 3.2 * a
  out.opacity = 0.85 + 0.15 * (0.5 * a + 0.5 * b)
  return out
}
