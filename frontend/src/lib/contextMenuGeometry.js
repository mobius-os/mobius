/* Keep a context menu beside its anchor and inside one shared layout viewport. */

function positiveNumber(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

function clamp(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum))
}

export function placeContextMenu({
  point,
  viewport,
  menuSize,
  gap = 8,
  padding = 12,
}) {
  const viewportWidth = positiveNumber(viewport?.width, 1)
  const viewportHeight = positiveNumber(viewport?.height, 1)
  const pointX = Number(point?.x) || 0
  const pointY = Number(point?.y) || 0
  const menuWidth = positiveNumber(menuSize?.width, 0)
  const menuHeight = positiveNumber(menuSize?.height, 0)
  const safeGap = Math.max(0, Number(gap) || 0)
  const safePadding = Math.max(0, Number(padding) || 0)

  let x = pointX + safeGap
  if (x + menuWidth > viewportWidth - safePadding) {
    x = pointX - menuWidth - safeGap
  }

  let y = pointY + safeGap
  if (y + menuHeight > viewportHeight - safePadding) {
    y = pointY - menuHeight - safeGap
  }

  return {
    x: clamp(x, safePadding, viewportWidth - menuWidth - safePadding),
    y: clamp(y, safePadding, viewportHeight - menuHeight - safePadding),
  }
}
