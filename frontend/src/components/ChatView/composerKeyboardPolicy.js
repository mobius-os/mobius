/**
 * Mobile submit keyboard policy.
 *
 * A normal send starts a turn, so dismissing the keyboard gives the reply the
 * viewport. A queue-only send is different: the owner is still composing
 * follow-ups, so keep the textarea focused. An explicit queue-and-steer submit
 * carries the same dismiss intent as tapping either fast-forward control.
 */
export function shouldDismissComposerKeyboardOnSubmit({
  isTouchPrimary = false,
  queuesBehindActiveTurn = false,
  steerAfterQueue = false,
} = {}) {
  return !!(
    isTouchPrimary
    && (!queuesBehindActiveTurn || steerAfterQueue)
  )
}
