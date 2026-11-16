/*
 * A key request card's answers (Not now, Allow) wait a moment before they
 * count, as a browser's permission prompt does: a card that appears under
 * the pointer (an app's request over its window, a card streamed into a
 * chat) must not take a click the owner aimed at what was there before.
 * KeyRequest.jsx applies it to every card: Settings › Keys, a chat and over
 * an app. Pure and unit-tested; times are the page's clock
 * (``performance.now()``, which an event's ``timeStamp`` shares).
 */

export const KEY_ANSWER_ARM_MS = 1000

/** How much longer a card shown at ``shownAt`` keeps its answers inactive at ``now``. */
export function keyAnswerWaitMs(shownAt, now) {
  return Math.max(0, shownAt + KEY_ANSWER_ARM_MS - now)
}

/**
 * Whether a click on an answer counts. A pointer click counts only when its
 * press began on that answer after the card's answers became active
 * (``pressAt``, null when no press on it was seen): a press that began
 * before the card appeared, or while it was still inactive, is ignored
 * however long it is held. A keyboard click (``byPointer`` false) needs only
 * the card to be active.
 */
export function keyAnswerClickCounts({ shownAt, clickAt, pressAt = null, byPointer }) {
  const activeAt = shownAt + KEY_ANSWER_ARM_MS
  return byPointer ? pressAt !== null && pressAt >= activeAt : clickAt >= activeAt
}
