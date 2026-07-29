/* Context-menu geometry bridges painted pointer coordinates to the root
   layout space, then keeps the menu beside the pointer and inside the viewport. */

function positiveNumber(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

function clamp(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum))
}

export function placeContextMenu({
  clientPoint,
  clientViewport,
  layoutViewport,
  menuSize,
  gap = 8,
  padding = 12,
}) {
  const clientWidth = positiveNumber(clientViewport?.width, 1)
  const clientHeight = positiveNumber(clientViewport?.height, 1)
  const layoutWidth = positiveNumber(layoutViewport?.width, clientWidth)
  const layoutHeight = positiveNumber(layoutViewport?.height, clientHeight)
  const scaleX = layoutWidth / clientWidth
  const scaleY = layoutHeight / clientHeight
  const pointX = (Number(clientPoint?.x) - (Number(clientViewport?.left) || 0)) * scaleX
  const pointY = (Number(clientPoint?.y) - (Number(clientViewport?.top) || 0)) * scaleY
  const menuWidth = positiveNumber(menuSize?.width, 0)
  const menuHeight = positiveNumber(menuSize?.height, 0)
  const gapX = Math.max(0, Number(gap) || 0) * scaleX
  const gapY = Math.max(0, Number(gap) || 0) * scaleY
  const paddingX = Math.max(0, Number(padding) || 0) * scaleX
  const paddingY = Math.max(0, Number(padding) || 0) * scaleY

  let x = pointX + gapX
  if (x + menuWidth > layoutWidth - paddingX) {
    x = pointX - menuWidth - gapX
  }

  let y = pointY + gapY
  if (y + menuHeight > layoutHeight - paddingY) {
    y = pointY - menuHeight - gapY
  }

  return {
    x: clamp(x, paddingX, layoutWidth - menuWidth - paddingX),
    y: clamp(y, paddingY, layoutHeight - menuHeight - paddingY),
  }
}
