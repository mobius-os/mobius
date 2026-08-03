/**
 * How tall the composer popover is allowed to be.
 *
 * The popover drops UP from the `+` trigger, so its usable height is the space
 * between the trigger's top edge and the nearest boundary above it. There are
 * TWO such boundaries and the fix needs the lower (more restrictive) one:
 *
 *   1. The popover's CLIPPING ANCESTOR. `.chat` is `overflow: hidden`, so
 *      anything the panel renders above the chat pane's top edge is clipped
 *      away — invisible AND untappable (hit-testing lands on the shell header
 *      painted there instead). The panel cannot escape by z-index either: the
 *      pane is its own stacking layer below the shell chrome, by design.
 *   2. The VISIBLE VIEWPORT top (`visualViewport`), for the case where the
 *      pane itself extends above the visible area.
 *
 * WHY NOT `dvh`: iOS Safari does not shrink the layout viewport — and therefore
 * not `dvh`/`vh` — when the soft keyboard opens; `interactive-widget` is a
 * Chromium feature. So `max-height: min(50dvh, 420px)` measured ~420px of
 * FULL-screen height while the keyboard had pushed the trigger up to ~1/3 of
 * the screen. The panel's top rows — Attach files first — landed outside the
 * pane and were unreachable: scrolling the panel moves content further up, and
 * the page behind it can't scroll to them either.
 *
 * ╔════════════════════════════════════════════════════════════════╗
 * ║                                                                ║
 * ║   THE TWO COORDINATE SPACES — iOS keyboard, read this first    ║
 * ║                                                                ║
 * ║   `getBoundingClientRect()` is defined against the LAYOUT       ║
 * ║   viewport and `visualViewport.offsetTop` measures the visible  ║
 * ║   viewport's offset FROM that same layout viewport, so on       ║
 * ║   paper subtracting one from the other is sound.                ║
 * ║                                                                ║
 * ║   It is not sound on iOS. The whole shell is                    ║
 * ║   `position: fixed; inset: 0` on an unscrollable document       ║
 * ║   (`html, body, #root { position: fixed; overflow: hidden }`),  ║
 * ║   and once the soft keyboard offsets the visual viewport iOS    ║
 * ║   reports client rects for that fixed layer relative to the     ║
 * ║   VISUAL viewport — the keyboard inset is already baked into    ║
 * ║   `triggerTop`. Subtracting `offsetTop` on top of that counts   ║
 * ║   the keyboard TWICE, drives `space` negative, and the panel    ║
 * ║   renders as a ~14px empty sliver (padding + border around a    ║
 * ║   zero-height scrollport). That is the "tapping + does nothing  ║
 * ║   while the keyboard is up" bug; it was intermittent because    ║
 * ║   whether the measurement caught the pre- or post-offset frame  ║
 * ║   depended on when iOS fired its viewport events.               ║
 * ║                                                                ║
 * ║   `visibleTopInRectSpace` below decides which space the rects   ║
 * ║   are in, using the one fact that is always true at measure     ║
 * ║   time: the user just TAPPED the trigger, so the trigger is     ║
 * ║   on screen. Don't replace it with a UA sniff, and don't        ║
 * ║   "simplify" it back to a bare `offsetTop` subtraction.         ║
 * ║                                                                ║
 * ╚════════════════════════════════════════════════════════════════╝
 */

/** Gap between the popover's bottom edge and the trigger (matches the CSS). */
export const POPOVER_GAP = 8
/** Breathing room so the panel never kisses the boundary above it. */
export const POPOVER_TOP_MARGIN = 8
/** Upper bound on tall screens — a full-height panel reads as a takeover. */
export const POPOVER_CAP = 420
// There is deliberately NO MINIMUM HEIGHT. A 160px floor was tried here and
// removed: a floor cannot be made safe. The quantity it must respect is
// `space` — the room between the trigger and the clipping boundary — and a
// floor bounded by `space` is by definition never larger than the measurement
// it exists to rescue, so it does nothing. Bounding it by the visible viewport
// instead is vacuous: on a phone the two differ by hundreds of pixels, so the
// floor rendered ~60px of the panel, the Attach row included, above the
// clipping ancestor — invisible and untappable, which is the exact failure
// this helper exists to prevent. `MIN_PANE_H` (200) also makes a genuinely
// cramped pane a SUPPORTED surface, not the impossible measurement a floor
// assumes. The sliver a floor was meant to rescue came from the
// coordinate-space double-count below: fix the measurement, don't pad it.

/**
 * Top edge (in client coordinates) of the nearest ancestor that would clip the
 * popover. Returns 0 when nothing clips, so the caller falls back to the
 * viewport boundary alone.
 *
 * `overflow: visible` is the only non-clipping value; `hidden`, `auto`,
 * `scroll`, and `clip` all cut the panel off. Checking the computed value
 * rather than hardcoding `.chat` keeps this correct for tiled panes, embedded
 * app chats, and any future container that wraps the composer.
 */
export function nearestClipTop(el) {
  let node = el?.parentElement || null
  while (node && node !== document.body) {
    const style = window.getComputedStyle(node)
    if (style.overflowY !== 'visible' || style.overflowX !== 'visible') {
      return node.getBoundingClientRect().top
    }
    node = node.parentElement
  }
  return 0
}

/**
 * Where the top of the VISIBLE viewport sits in the caller's normalized
 * coordinate space. See the coordinate-spaces box above.
 *
 * The trigger was just tapped, so it is on screen. If its rect already fits
 * inside a band that starts at 0 and is `viewportHeight` tall, the rects are
 * visual-viewport-relative and the keyboard inset is already in them — the
 * visible top is 0 and `offsetTop` must NOT be applied again. If the rect sits
 * below that band, the rects are layout-viewport-relative (the spec-correct
 * case, and what Android reports), so `offsetTop` is the visible top.
 *
 * @param {number} triggerBottom  normalized trigger bottom
 * @param {number} viewportTop    normalized visual viewport offset
 * @param {number} viewportHeight normalized visual viewport height
 * @returns {number}
 */
export function visibleTopInRectSpace({ triggerBottom, viewportTop, viewportHeight }) {
  if (!(viewportTop > 0)) return 0
  if (!(viewportHeight > 0)) return viewportTop
  return triggerBottom <= viewportHeight ? 0 : viewportTop
}

/**
 * @param {object} m
 * @param {number} m.triggerTop        normalized trigger top
 * @param {number} [m.triggerBottom]   normalized trigger bottom
 * @param {number} [m.viewportTop]     normalized viewport offset (0 when absent)
 * @param {number} [m.viewportHeight]  normalized viewport height (0 when absent)
 * @param {number} [m.clipTop]         normalized clipping top (0 when none)
 * @param {number} [m.cap]
 * @returns {number} max-height in CSS pixels
 */
export function popoverMaxHeight({
  triggerTop,
  triggerBottom = triggerTop,
  viewportTop = 0,
  viewportHeight = 0,
  clipTop = 0,
  cap = POPOVER_CAP,
}) {
  const visibleTop = visibleTopInRectSpace({ triggerBottom, viewportTop, viewportHeight })
  // Floor the boundary at 0: when the rects are visual-viewport-relative the
  // clipping pane's top is NEGATIVE (it starts above the visible area), and the
  // real boundary is the top of the screen, not that negative number.
  const boundary = Math.max(0, visibleTop, clipTop)
  const space = triggerTop - boundary - POPOVER_GAP - POPOVER_TOP_MARGIN
  // Never more than the measured space: the panel must stay inside the clipping
  // ancestor even when that leaves it very short. See the no-minimum note above.
  return Math.min(cap, Math.floor(Math.max(0, space)))
}
