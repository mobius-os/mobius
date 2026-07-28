/* Intrinsic layout metadata for chat Markdown images. */

const FRAME_MAX_WIDTH = 520
const FRAME_MIN_WIDTH = 120
const FRAME_CAP_H = 480
const FRAME_VIEWPORT_FRACTION = 0.6
const DEFAULT_VIEWPORT_H = 800

export function imageDimensionsForHref(href, mediaDimensions) {
  if (!href || typeof href !== 'string' || !mediaDimensions) return null
  let pathname
  try {
    pathname = new URL(href, 'https://mobius.local').pathname
  } catch {
    return null
  }
  const value = mediaDimensions[pathname]
  if (!value || !Number.isInteger(value.width) || !Number.isInteger(value.height)) {
    return null
  }
  const { width, height } = value
  if (width <= 0 || height <= 0) return null
  return { width, height }
}

/**
 * Builds the exact first-layout custom properties from server-read dimensions.
 *
 * @param {number} width
 * @param {number} height
 * @param {number} [viewportH]  visual-viewport height for the height cap
 * @returns {object|null}  a React style object, or null for invalid dims
 */
export function imageVarsFromDims(width, height, viewportH) {
  if (!(width > 0) || !(height > 0)) return null
  const ratio = width / height
  const vh = Number.isFinite(viewportH) && viewportH > 0
    ? viewportH
    : DEFAULT_VIEWPORT_H
  const cappedH = Math.min(vh * FRAME_VIEWPORT_FRACTION, FRAME_CAP_H)
  const fitWidth = Math.min(
    FRAME_MAX_WIDTH,
    Math.max(FRAME_MIN_WIDTH, Math.round(cappedH * ratio)),
  )
  return {
    '--md-image-ratio': `${width} / ${height}`,
    '--md-image-fit-width': `${fitWidth}px`,
  }
}
