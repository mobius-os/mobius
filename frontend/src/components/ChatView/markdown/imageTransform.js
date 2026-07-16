/** Pure geometry helpers for the chat image lightbox. */

export const MIN_IMAGE_SCALE = 1
export const MAX_IMAGE_SCALE = 5

export function clampImageScale(scale) {
  return Math.min(MAX_IMAGE_SCALE, Math.max(MIN_IMAGE_SCALE, scale))
}

export function clampImageTransform(transform, metrics) {
  const scale = clampImageScale(transform.scale)
  if (scale <= 1) return { scale: 1, x: 0, y: 0 }

  const maxX = Math.max(0, (metrics.baseWidth * scale - metrics.viewportWidth) / 2)
  const maxY = Math.max(0, (metrics.baseHeight * scale - metrics.viewportHeight) / 2)
  return {
    scale,
    x: Math.min(maxX, Math.max(-maxX, transform.x)),
    y: Math.min(maxY, Math.max(-maxY, transform.y)),
  }
}

export function zoomImageAround(transform, nextScale, point, baseCenter, metrics) {
  const scale = clampImageScale(nextScale)
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
