// Immersive safe-area passthrough (.pm/128 follow-up).
//
// A sandboxed mini-app iframe cannot read the device's notch / home-indicator
// insets: env(safe-area-inset-*) resolves to 0 inside the iframe because only
// the TOP-LEVEL document participates in viewport-fit=cover inset resolution.
// So an immersive (full-bleed, under-the-notch) app has no way to pad its own
// UI away from the notch — its content slides under the cutout.
//
// The shell DOES see the real insets. This module is the pure core of the
// passthrough: it reads the four env(safe-area-inset-*) values off a probe
// element the SHELL owns, and the active app's AppCanvas forwards them to the
// iframe as a moebius:frame-insets message. The frame applies them to :root as
// --mobius-safe-{top,right,bottom,left} custom properties, which immersive
// apps reference instead of env() (which reads 0 for them).
//
// Only forwarded while the app is immersive; non-immersive apps get zeros (the
// shell chrome already owns the inset padding then, so the app must NOT
// double-pad). Keeping this a pure px-string reader makes it unit-testable
// without a real layout engine — the caller supplies the computed style.

// Read the four insets from a probe element whose inline style sets each
// padding side to env(safe-area-inset-*). getComputedStyle resolves the env()
// to a concrete px value on the top-level document. Returns an object of CSS
// length strings ('0px' when the engine reports nothing), never null, so the
// caller always has a complete set to forward.
export function readSafeAreaInsets(computedStyle) {
  return {
    top: normalizeInset(computedStyle?.paddingTop),
    right: normalizeInset(computedStyle?.paddingRight),
    bottom: normalizeInset(computedStyle?.paddingBottom),
    left: normalizeInset(computedStyle?.paddingLeft),
  }
}

// The all-zero insets a non-immersive app receives so it never double-pads on
// top of the shell chrome's own safe-area padding. Also the value posted when
// an app RELEASES immersive, resetting its --mobius-safe-* back to 0.
export function zeroInsets() {
  return { top: '0px', right: '0px', bottom: '0px', left: '0px' }
}

// A blank / 'auto' / negative computed padding reads as no inset. Browsers
// report resolved env() insets as non-negative px strings; anything else is
// treated as 0 so a malformed probe can't push content the wrong way.
function normalizeInset(value) {
  if (typeof value !== 'string') return '0px'
  const px = parseFloat(value)
  if (!Number.isFinite(px) || px <= 0) return '0px'
  return `${px}px`
}
