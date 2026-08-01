/* Constant-size drawer windowing for the mixed Recent list. */

export const DRAWER_ROW_HEIGHT = 44
export const DRAWER_ROW_OVERSCAN = 8
export const DRAWER_INITIAL_WINDOW_ROWS = 48

function boundedTotal(total) {
  return Math.max(0, Number.isFinite(total) ? Math.floor(total) : 0)
}

export function initialDrawerRowWindow(total) {
  const count = boundedTotal(total)
  return { start: 0, end: Math.min(count, DRAWER_INITIAL_WINDOW_ROWS) }
}

export function clampDrawerRowWindow(window, total) {
  const count = boundedTotal(total)
  if (count === 0) return { start: 0, end: 0 }
  const width = Math.max(
    1,
    Number.isFinite(window?.end - window?.start)
      ? Math.floor(window.end - window.start)
      : DRAWER_INITIAL_WINDOW_ROWS,
  )
  const start = Math.min(
    Math.max(0, Math.floor(window?.start || 0)),
    Math.max(0, count - width),
  )
  return {
    start,
    end: Math.min(count, Math.max(start + 1, start + width)),
  }
}

/** Resolve the rows around the real scroll viewport, with fixed overscan above
 * and below. `sectionTop` is the Recent section's content-space Y inside the
 * drawer scroller; top and bottom spacers preserve the list's exact extent. */
export function drawerRowWindow({
  total,
  scrollTop,
  viewportHeight,
  sectionTop,
  rowHeight = DRAWER_ROW_HEIGHT,
  overscan = DRAWER_ROW_OVERSCAN,
}) {
  const count = boundedTotal(total)
  if (count === 0) return { start: 0, end: 0 }
  const height = Math.max(1, Number(rowHeight) || DRAWER_ROW_HEIGHT)
  const localTop = Math.max(0, (Number(scrollTop) || 0) - (Number(sectionTop) || 0))
  const visibleStart = Math.floor(localTop / height)
  const visibleEnd = Math.ceil(
    (localTop + Math.max(height, Number(viewportHeight) || 0)) / height,
  )
  return {
    start: Math.max(0, visibleStart - overscan),
    end: Math.min(count, Math.max(visibleStart + 1, visibleEnd + overscan)),
  }
}

export function drawerRowWindowContaining(total, rowIndex, windowRows = DRAWER_INITIAL_WINDOW_ROWS) {
  const count = boundedTotal(total)
  if (!Number.isInteger(rowIndex) || rowIndex < 0 || rowIndex >= count) {
    return initialDrawerRowWindow(count)
  }
  const width = Math.max(1, Math.min(count, Math.floor(windowRows)))
  const before = Math.floor(width / 3)
  const start = Math.min(Math.max(0, rowIndex - before), Math.max(0, count - width))
  return { start, end: Math.min(count, start + width) }
}

/** Return the existing window when the row is already mounted or is not part
 * of Recents. Pinned rows deliberately have a Recent index of -1: preserving
 * object identity there prevents a layout effect from scheduling forever. */
export function drawerRowWindowForIndex(current, total, rowIndex) {
  const count = boundedTotal(total)
  if (!Number.isInteger(rowIndex) || rowIndex < 0 || rowIndex >= count) return current
  if (rowIndex >= current?.start && rowIndex < current?.end) return current
  return drawerRowWindowContaining(count, rowIndex)
}

export function drawerRowSpacerHeights(window, total, rowHeight = DRAWER_ROW_HEIGHT) {
  const bounded = clampDrawerRowWindow(window, total)
  return {
    before: bounded.start * rowHeight,
    after: Math.max(0, boundedTotal(total) - bounded.end) * rowHeight,
  }
}

export function sameDrawerRowWindow(left, right) {
  return left?.start === right?.start && left?.end === right?.end
}
