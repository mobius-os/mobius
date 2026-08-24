/** Pure geometry helpers for the chat image lightbox. */

export const MIN_IMAGE_SCALE = 1
export const MAX_IMAGE_SCALE = 5

// Mirrors the desktop .lightbox-image gutter in lightbox.css so the reading
// zoom lands on the same fitted width the stylesheet gives the image.
const LIGHTBOX_GUTTER = 32

/** `maxScale` is a true ceiling: callers may raise it above the 5× default or
 * lower it for a constrained mode. */
export function clampImageScale(scale, maxScale = MAX_IMAGE_SCALE) {
  return Math.min(maxScale, Math.max(MIN_IMAGE_SCALE, scale))
}

/** The zoom ceiling for a fitted image. 5× of the fitted size covers ordinary
 * images; a very long screenshot fits the screen as a thin strip, so its
 * ceiling extends to true 1:1 device pixels (`naturalWidth` painted across
 * `baseWidth × dpr` device pixels) or small text could never become readable.
 * Metrics without the natural measurements keep the default ceiling. */
export function imageScaleCeiling(metrics) {
  const dpr = metrics.dpr || 1
  if (!(metrics.baseWidth > 0) || !(metrics.naturalWidth > 0)) return MAX_IMAGE_SCALE
  return Math.max(MAX_IMAGE_SCALE, metrics.naturalWidth / (metrics.baseWidth * dpr))
}

function panLimits(scale, metrics) {
  return {
    maxX: Math.max(0, (metrics.baseWidth * scale - metrics.viewportWidth) / 2),
    maxY: Math.max(0, (metrics.baseHeight * scale - metrics.viewportHeight) / 2),
  }
}

/** Pan engages whenever there is room, not merely when zoomed: the viewport
 * here is the visual viewport, so a tall image fitted to the layout viewport
 * still has room at 1× while a mobile browser toolbar hides its ends, and a
 * small image zoomed within the screen has none. */
export function hasPanRoom(scale, metrics) {
  const { maxX, maxY } = panLimits(scale, metrics)
  return maxX > 0 || maxY > 0
}

export function clampImageTransform(transform, metrics) {
  const scale = clampImageScale(transform.scale, imageScaleCeiling(metrics))
  const { maxX, maxY } = panLimits(scale, metrics)
  // + 0 normalises the -0 a zero-room clamp produces, so a recentred
  // transform compares equal to the reset state.
  return {
    scale,
    x: Math.min(maxX, Math.max(-maxX, transform.x)) + 0,
    y: Math.min(maxY, Math.max(-maxY, transform.y)) + 0,
  }
}

export function zoomImageAround(transform, nextScale, point, baseCenter, metrics) {
  const scale = clampImageScale(nextScale, imageScaleCeiling(metrics))
  if (scale <= 1) return { scale: 1, x: 0, y: 0 }

  // Preserve the image-space point beneath the cursor/fingers. This is what
  // makes a wheel or pinch feel attached to the thing the owner is examining
  // instead of zooming vaguely toward the centre of the screen.
  const imageX = (point.x - baseCenter.x - transform.x) / transform.scale
  const imageY = (point.y - baseCenter.y - transform.y) / transform.scale
  return clampImageTransform({
    scale,
    x: point.x - baseCenter.x - imageX * scale,
    y: point.y - baseCenter.y - imageY * scale,
  }, metrics)
}

/** True when a plain scroll should pan the image: it has pan room on the
 * scroll's dominant axis. A fully visible image, or one with no room that way
 * (a wide image scrolled vertically), falls through to zoom so wheel input is
 * never dead. Only the deltas' relative magnitude matters, so client- or
 * layout-space deltas both work. */
export function wheelScrollPans(transform, deltaX, deltaY, metrics) {
  const { maxX, maxY } = panLimits(transform.scale, metrics)
  return Math.abs(deltaY) >= Math.abs(deltaX) ? maxY > 0 : maxX > 0
}

/** Double-click target for a fitted image: ordinary images keep the classic
 * 2×; a tall strip jumps to reading width — the viewport less the stylesheet
 * gutter — but never past its own legible 1:1 device pixels, so a small image
 * is not blown up into blur. */
export function readingZoomScale(metrics) {
  if (!(metrics.baseWidth > 0)) return 2
  const fitWidth = Math.max(0, metrics.viewportWidth - LIGHTBOX_GUTTER) / metrics.baseWidth
  const native = metrics.naturalWidth > 0
    ? metrics.naturalWidth / (metrics.baseWidth * (metrics.dpr || 1))
    : 2
  return Math.max(2, Math.min(fitWidth, native))
}
