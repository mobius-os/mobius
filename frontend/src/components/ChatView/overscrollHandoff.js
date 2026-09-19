/**
 * Scroll-chaining handoff for a nested, capped field inside the transcript.
 *
 * A `<textarea>` such as the Q&A custom-answer box grows to a cap and then
 * scrolls its own overflow. On Chromium a wheel/drag that reaches the field's
 * edge chains the leftover scroll to the ancestor scroller, but iOS/WebKit
 * never chains a nested scroller — the field traps the gesture at its edge and
 * the conversation stops moving until you lift and swipe outside the box. This
 * predicate says how much of a gesture's delta the transcript should absorb so
 * scrolling continues past the field's edge on every engine.
 *
 * @param {object} m
 * @param {number} m.delta        signed gesture delta; +down / -up in px
 * @param {number} m.scrollTop    the field's current scrollTop
 * @param {number} m.scrollHeight the field's full scroll height
 * @param {number} m.clientHeight the field's visible height
 * @returns {number} px to hand to the outer scroller, or 0 when the field can
 *   still consume the gesture itself (native chaining already works there)
 */
export function overscrollHandoffDelta({
  delta,
  scrollTop,
  scrollHeight,
  clientHeight,
} = {}) {
  if (![delta, scrollTop, scrollHeight, clientHeight].every(Number.isFinite)) {
    return 0
  }
  if (delta === 0) return 0
  const maxScroll = Math.max(0, scrollHeight - clientHeight)
  // A field with nothing to scroll never captures the gesture, so native
  // chaining already reaches the transcript — leave that path alone.
  if (maxScroll <= 0.5) return 0
  if (delta > 0) return scrollTop >= maxScroll - 0.5 ? delta : 0
  return scrollTop <= 0.5 ? delta : 0
}
