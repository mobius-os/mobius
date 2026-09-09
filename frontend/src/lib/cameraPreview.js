function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

// Camera previews stay in the trusted shell. An opaque app may request where
// its viewfinder sits, but it never receives the MediaStream or an unrestricted
// styling surface. Invalid geometry hides the preview rather than letting a
// hostile or stale frame place shell-owned video unpredictably.
export function readCameraPreviewRect(value) {
  if (!value || typeof value !== 'object') return null
  const { x, y, width, height } = value
  if (![x, y, width, height].every(finiteNumber)) return null
  if (width <= 0 || height <= 0) return null
  return { x, y, width, height }
}

export function clampCameraPreviewRect(value, bounds) {
  const rect = readCameraPreviewRect(value)
  const boundWidth = bounds?.width
  const boundHeight = bounds?.height
  if (!rect || !finiteNumber(boundWidth) || !finiteNumber(boundHeight)) return null
  if (boundWidth <= 0 || boundHeight <= 0) return null

  const left = Math.max(0, Math.min(boundWidth, rect.x))
  const top = Math.max(0, Math.min(boundHeight, rect.y))
  const right = Math.max(0, Math.min(boundWidth, rect.x + rect.width))
  const bottom = Math.max(0, Math.min(boundHeight, rect.y + rect.height))
  if (![left, top, right, bottom].every(Number.isFinite)) return null
  if (right - left < 1 || bottom - top < 1) return null

  return {
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
  }
}
