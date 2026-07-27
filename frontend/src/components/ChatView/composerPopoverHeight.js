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
 */

/** Gap between the popover's bottom edge and the trigger (matches the CSS). */
export const POPOVER_GAP = 8
/** Breathing room so the panel never kisses the boundary above it. */
export const POPOVER_TOP_MARGIN = 8
/** Upper bound on tall screens — a full-height panel reads as a takeover. */
export const POPOVER_CAP = 420

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
 * @param {object} m
 * @param {number} m.triggerTop     trigger's `getBoundingClientRect().top`
 * @param {number} [m.viewportTop]  `visualViewport.offsetTop` (0 when absent)
 * @param {number} [m.clipTop]      `nearestClipTop(trigger)` (0 when none)
 * @param {number} [m.cap]
 * @returns {number} max-height in CSS pixels
 */
export function popoverMaxHeight({
  triggerTop,
  viewportTop = 0,
  clipTop = 0,
  cap = POPOVER_CAP,
}) {
  const boundary = Math.max(viewportTop, clipTop)
  const space = triggerTop - boundary - POPOVER_GAP - POPOVER_TOP_MARGIN
  // A minimum would knowingly cross the clipping boundary in the exact cramped
  // geometry this helper exists to make safe. A short scrollport is imperfect
  // but reachable; overflowing above the pane makes its first actions impossible.
  return Math.max(0, Math.min(cap, Math.floor(space)))
}
