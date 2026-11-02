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

/** Place a popover centred under an anchor box, or over it when there is no room below, inside `bounds`. */
export function placeUnderAnchor({
  anchor,
  bounds,
  size,
  gap = 8,
  padding = 12,
}) {
  const width = positiveNumber(size?.width, 0)
  const height = positiveNumber(size?.height, 0)
  const left = Number(bounds?.left) || 0
  const top = Number(bounds?.top) || 0
  const right = positiveNumber(bounds?.right, left + 1)
  const bottom = positiveNumber(bounds?.bottom, top + 1)
  const centre = (Number(anchor?.left) + Number(anchor?.right)) / 2 || 0
  const below = (Number(anchor?.bottom) || 0) + gap
  const above = (Number(anchor?.top) || 0) - height - gap
  const y = below + height > bottom - padding && above >= top + padding ? above : below
  return {
    x: clamp(centre - width / 2, left + padding, right - width - padding),
    y: clamp(y, top + padding, bottom - height - padding),
  }
}
