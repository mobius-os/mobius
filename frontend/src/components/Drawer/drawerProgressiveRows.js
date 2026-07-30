/* Progressive drawer-row sizing keeps one continuous list without mounting it all. */

export const DRAWER_ROW_BATCH_SIZE = 48

export function initialDrawerRowCount(total) {
  return Math.min(Math.max(0, total), DRAWER_ROW_BATCH_SIZE)
}

export function nextDrawerRowCount(current, total) {
  const boundedTotal = Math.max(0, total)
  return Math.min(
    boundedTotal,
    Math.max(0, current) + DRAWER_ROW_BATCH_SIZE,
  )
}

export function clampDrawerRowCount(current, total) {
  return Math.min(
    Math.max(initialDrawerRowCount(total), current),
    Math.max(0, total),
  )
}

export function drawerRowCountToReveal(current, total, rowIndex) {
  const boundedCurrent = clampDrawerRowCount(current, total)
  if (!Number.isInteger(rowIndex) || rowIndex < 0 || rowIndex >= total) {
    return boundedCurrent
  }
  return Math.max(boundedCurrent, rowIndex + 1)
}
