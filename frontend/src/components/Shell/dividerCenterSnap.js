/* Screen-center snap math for Builder pane dividers. */

export const CENTER_SNAP_ENTER_PX = 14
export const CENTER_SNAP_RELEASE_PX = 26

function finite(value) {
  if (value == null) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function axisGeometry(divider, contentRect) {
  const row = divider?.dir === 'row'
  const col = divider?.dir === 'col'
  if (!row && !col) return null
  const origin = finite(divider.origin)
  const span = finite(divider.span)
  const gap = finite(row ? divider.w : divider.h)
  const rawContentOrigin = row ? contentRect?.x : contentRect?.y
  // Shell layout rects ordinarily carry only {w, h}; paneModel's geometry owner
  // defines an omitted x/y as the content-local zero origin.
  const contentOrigin = rawContentOrigin == null ? 0 : finite(rawContentOrigin)
  const contentSpan = finite(row ? contentRect?.w : contentRect?.h)
  if (origin == null || span == null || span <= 0 || gap == null
      || contentOrigin == null || contentSpan == null || contentSpan <= 0) return null
  return { origin, span, gap, contentOrigin, contentSpan }
}

// Ratio that places the divider's visible hairline on the content viewport's
// global midpoint. Nested dividers therefore snap to the screen center only when
// their own parent region genuinely crosses it, rather than mistaking their local
// 50/50 point for the middle of the screen.
export function screenCenterRatio(divider, contentRect) {
  const geometry = axisGeometry(divider, contentRect)
  if (!geometry) return null
  const { origin, span, gap, contentOrigin, contentSpan } = geometry
  const target = (contentOrigin + contentSpan / 2 - origin - gap / 2) / span
  return target >= 0 && target <= 1 ? target : null
}

export function dividerDistanceFromScreenCenter(divider, contentRect) {
  const geometry = axisGeometry(divider, contentRect)
  const ratio = finite(divider?.ratio)
  if (!geometry || ratio == null) return Infinity
  const { origin, span, gap, contentOrigin, contentSpan } = geometry
  const dividerCenter = origin + span * ratio + gap / 2
  return Math.abs(dividerCenter - (contentOrigin + contentSpan / 2))
}

// Hysteresis makes the midpoint feel sticky instead of flickering on/off under a
// finger: enter within 14px, then require 26px of travel to pull free.
export function snapRatioToScreenCenter(rawRatio, targetRatio, span, wasSnapped = false) {
  const raw = finite(rawRatio)
  const target = finite(targetRatio)
  const axisSpan = finite(span)
  if (raw == null || target == null || axisSpan == null || axisSpan <= 0) {
    return { ratio: rawRatio, snapped: false }
  }
  const threshold = wasSnapped ? CENTER_SNAP_RELEASE_PX : CENTER_SNAP_ENTER_PX
  if (Math.abs(raw - target) * axisSpan <= threshold + 1e-6) {
    return { ratio: target, snapped: true }
  }
  return { ratio: raw, snapped: false }
}
