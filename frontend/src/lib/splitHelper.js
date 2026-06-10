// Pure logic for the window.mobius.split() state machine.
//
// Extracted here so the state transitions and thresholds are unit-testable
// without a browser DOM (mobius-runtime.js is served verbatim from /public and
// can't import a Vite /src module; this module is bundled into neither — it is
// the testable half. The runtime mirrors the constants and imports nothing from
// here; keep the two in sync the same way chatEmbed.js ↔ its runtime mirror).
//
// Three states: 'pill' | 'split' | 'full'.
//   pill  — content full height, 36px pill at bottom anchors chat; only on
//            viewports < 600px.
//   split — content pane + chat pane share the mount; ratio is the fraction of
//            height (or width in side-by-side mode) the CONTENT pane takes.
//   full  — chat fills the mount; content pane has 0 height.
//
// Ratio semantics: always the CONTENT fraction (0–1).
//   ratio 0.65 → content 65%, chat 35%.
//   pill → content 100% (chat 0, pill overlay).
//   full → content 0%, chat 100%.

export const STATES = Object.freeze({ PILL: 'pill', SPLIT: 'split', FULL: 'full' })
export const WIDE_BREAKPOINT_PX = 600
export const FLICK_VELOCITY_PX_MS = 0.4   // px/ms: above this = velocity-flick
export const DEAD_ZONE_PX = 24             // pointer must travel >24px before drag counts
export const ARROW_STEP_RATIO = 0.04       // 4% per ArrowUp/Down key step

// Clamp ratio to keep both panes usable.
// `minContentPx` and `minChatPx` in pixels; `totalPx` is the available axis.
// Returns a ratio in [0, 1] (extremes allowed only when explicitly requested via
// transitions to 'pill'/'full', never from drag clamping alone).
export function clampRatio(ratio, totalPx, minContentPx, minChatPx) {
  if (totalPx <= 0) return ratio
  const lo = minContentPx / totalPx
  const hi = 1 - minChatPx / totalPx
  if (hi < lo) return 0.5          // mount too small to satisfy both minimums
  return Math.min(hi, Math.max(lo, ratio))
}

// Decide which state to transition to after a drag/flick ends.
// `ratio`     — the raw ratio the pointer landed on (0–1, content fraction).
// `velocity`  — signed px/ms (positive = growing the content pane, i.e. shrinking chat).
// `wide`      — true when viewport is ≥ WIDE_BREAKPOINT_PX (no pill state available).
// `totalPx`   — height (portrait) or width (landscape) of the mount.
// `minContentPx`, `minChatPx` — from opts.
// Returns 'pill' | 'split' | 'full'.
export function resolveTransition(ratio, velocity, wide, totalPx, minContentPx, minChatPx) {
  // Flick toward full (content→0): negative velocity above threshold.
  if (velocity < -FLICK_VELOCITY_PX_MS) return STATES.FULL
  // Flick toward pill/content-full: positive velocity above threshold.
  if (velocity > FLICK_VELOCITY_PX_MS) return wide ? STATES.SPLIT : STATES.PILL

  // No flick — snap by position.
  const clampedRatio = clampRatio(ratio, totalPx, minContentPx, minChatPx)
  // If dragged past the clamp minimum for content, go full.
  if (clampedRatio <= minContentPx / totalPx + 0.01) return STATES.FULL
  // If dragged past the clamp minimum for chat (content taking almost everything), go pill/split.
  if (clampedRatio >= 1 - minChatPx / totalPx - 0.01) return wide ? STATES.SPLIT : STATES.PILL
  // Middle ground: stay split.
  return STATES.SPLIT
}

// Map a state to the CSS custom property value for --cs-content-h (vertical mode).
// `ratio` is only used for 'split'; `totalPx` is the mount height.
export function stateToContentHeight(state, ratio, totalPx) {
  if (state === STATES.PILL) return totalPx     // content full, pill floats over
  if (state === STATES.FULL) return 0           // chat full
  return Math.round(ratio * totalPx)            // split: content fraction
}

// Map a state to the CSS custom property for --cs-content-w (horizontal/side mode).
export function stateToContentWidth(state, ratio, totalPx) {
  if (state === STATES.FULL) return 0
  return Math.round(ratio * totalPx)
}

// Parse a persisted {ratio, state} object from sessionStorage. Returns null on
// any shape mismatch so the caller falls back to defaults.
export function parsePersisted(raw) {
  if (!raw || typeof raw !== 'object') return null
  const { ratio, state } = raw
  if (typeof ratio !== 'number' || ratio < 0 || ratio > 1) return null
  if (!Object.values(STATES).includes(state)) return null
  return { ratio, state }
}
