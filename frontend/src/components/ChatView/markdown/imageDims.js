/*
 * Known-dimension parsing for chat markdown images (contract v2 item 2, lever
 * 3). When the agent embeds an image whose size it already knows — a screenshot
 * taken at the viewport it was handed — it can carry that size in the image
 * href as `?w=<int>&h=<int>`. The frame then reserves the exact aspect ratio on
 * the FIRST paint, so the box does not resize when the image decodes and the
 * on-load ratio delta disappears. Absent dims, the frame falls back to the CSS
 * 4/3 default and `onLoad` measures the real ratio as before.
 *
 * Pure + DOM-free so it unit-tests without React (see mediaImageEmbed.test.js).
 * The fit-width math mirrors ExpandableImage's onLoad handler exactly, so when
 * the carried dims equal the natural dims the two agree and nothing shifts.
 */

const FRAME_MAX_WIDTH = 520
const FRAME_MIN_WIDTH = 120
const FRAME_CAP_H = 480
const FRAME_VIEWPORT_FRACTION = 0.6
const DEFAULT_VIEWPORT_H = 800

/**
 * Extracts known pixel dimensions from an image href's `?w=&h=` query.
 *
 * @param {string} href  raw href from the markdown image token
 * @returns {{width:number, height:number}|null}  positive integer dims, or null
 */
export function parseImageDims(href) {
  if (!href || typeof href !== 'string') return null
  let params
  try {
    // Root the (possibly relative) href so URLSearchParams can read the query.
    // The origin is discarded — only the query matters here.
    params = new URL(href, 'https://mobius.local').searchParams
  } catch {
    return null
  }
  const width = Number.parseInt(params.get('w'), 10)
  const height = Number.parseInt(params.get('h'), 10)
  if (!Number.isFinite(width) || !Number.isFinite(height)) return null
  if (width <= 0 || height <= 0) return null
  return { width, height }
}

/**
 * Builds the `--md-image-ratio` / `--md-image-fit-width` custom-property object
 * the frame reads, from known dimensions. Identical formula to the onLoad
 * measurement, so a correct carried size produces no on-load delta.
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
