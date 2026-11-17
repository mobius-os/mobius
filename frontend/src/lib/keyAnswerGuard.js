/*
 * An owner's answer drawn where something else may have been a moment ago
 * counts only after it has been seen, still and untouched for a moment, as a
 * browser's permission prompt does: a card that appears, moves or is
 * revealed under the pointer (an app's request over its window, a card
 * streamed or pushed into place in a chat, a Settings card sliding up when
 * the one above leaves, a tab brought back) must not take a click or key the
 * owner aimed at what was there before, and steady clicking or typing must
 * not wear the wait down. useKeyAnswerGuard.js feeds it the events of every
 * key request card (Settings › Keys, a chat, over an app) and of the shell's
 * confirmation of an app's sensitive action (OwnerConfirmSheet.jsx). Pure
 * and unit-tested; times are the page's clock (``performance.now()``, which
 * an event's ``timeStamp`` shares).
 *
 * The state is ``{ quietSince, press }``: the answers wake KEY_ANSWER_ARM_MS
 * after ``quietSince`` (when the card appeared, last moved or was revealed,
 * or was last pressed anywhere), and ``press`` is the latest press on the
 * card, with whether it began while the answers were awake.
 */

export const KEY_ANSWER_ARM_MS = 1000

/** A card shown at ``at``, not yet pressed. */
export function keyAnswerShown(at) {
  return { quietSince: at, press: null }
}

/** When the answers wake. */
export function keyAnswerActiveAt(state) {
  return state.quietSince + KEY_ANSWER_ARM_MS
}

/**
 * A press anywhere on the card: its capture-phase ``pointerdown``, or a
 * ``keydown`` that is not a held key's repeat. It may answer only if the
 * answers were already awake, and it starts the wait again, so presses less
 * than a second apart never answer.
 */
export function keyAnswerPressed(state, event) {
  const at = event.timeStamp
  return {
    quietSince: Math.max(state.quietSince, at),
    press: { target: event.target, awake: at >= keyAnswerActiveAt(state) },
  }
}

/**
 * The card moved on screen, or came back into view, at ``at``: the wait
 * starts again, and a press already down no longer answers.
 */
export function keyAnswerMoved(state, at) {
  return {
    quietSince: Math.max(state.quietSince, at),
    press: state.press && { ...state.press, awake: false },
  }
}

/**
 * Whether a click on an answer counts: only when the latest press on the
 * card began on that answer while the answers were awake, so a press that
 * began before the card appeared, moved or was revealed never answers
 * however long it is held, and neither do keys pressed in a hurry (Tab to
 * reach the answer, then Enter). A click with no press at all since the card
 * appeared is assistive technology activating the answer, which sends no
 * pointer or key events: it is held to the wait alone. A pointer click
 * (``detail`` > 0) always has its press.
 */
export function keyAnswerClickCounts(state, event) {
  if (state.press) return Boolean(state.press.awake && event.currentTarget.contains(state.press.target))
  return event.detail === 0 && event.timeStamp >= keyAnswerActiveAt(state)
}

/** The card's place on screen, from ``getBoundingClientRect()``. */
export function keyAnswerPlace(rect) {
  return { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
}

/** Whether the card is somewhere else (or another size) than when last seen; a first sighting is not a move. */
export function keyAnswerPlaceChanged(before, after) {
  return Boolean(before) && (before.top !== after.top || before.left !== after.left
    || before.width !== after.width || before.height !== after.height)
}
