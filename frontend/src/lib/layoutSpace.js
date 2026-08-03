/* Bridge viewport/client geometry into the CSS layout space that inline styles consume. */

function finiteNumber(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

function positiveNumber(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

function firstPositive(...values) {
  for (const value of values) {
    const number = positiveNumber(value)
    if (number) return number
  }
  return 0
}

function computedEffectiveZoom(element) {
  if (typeof getComputedStyle !== 'function' || !element) return 0
  let node = element
  let zoom = 1
  let found = false
  while (node) {
    try {
      const raw = getComputedStyle(node)?.zoom
      const value = positiveNumber(raw)
      if (value) {
        zoom *= value
        found = true
      }
    } catch {
      return 0
    }
    node = node.parentElement || node.getRootNode?.().host || null
  }
  return found ? zoom : 0
}

/**
 * Capture one axis-aligned element's padding box (the document box for <html>)
 * as both viewport/client geometry and local CSS-layout geometry. CSS `zoom`
 * deliberately makes those spaces differ:
 * getBoundingClientRect() includes the effective zoom while offset* dimensions
 * do not. Keeping that fact in one value prevents pointer, viewport, and fixed
 * overlay code from each inventing its own scale calculation.
 */
export function captureLayoutSpace(element, fallbackRect = null) {
  const rect = element?.getBoundingClientRect?.() || fallbackRect || {}
  const effectiveZoom = positiveNumber(element?.currentCSSZoom)
    || computedEffectiveZoom(element)
  const cssZoom = effectiveZoom || 1
  const rectWidth = finiteNumber(rect?.width)
  const rectHeight = finiteNumber(rect?.height)
  const borderLayoutWidth = positiveNumber(element?.offsetWidth)
  const borderLayoutHeight = positiveNumber(element?.offsetHeight)
  const innerLayoutWidth = positiveNumber(element?.clientWidth)
  const innerLayoutHeight = positiveNumber(element?.clientHeight)
  const isDocumentRoot = Boolean(
    element && element.ownerDocument?.documentElement === element,
  )
  const layoutWidth = isDocumentRoot
    ? firstPositive(borderLayoutWidth, innerLayoutWidth, rectWidth / cssZoom)
    : firstPositive(innerLayoutWidth, borderLayoutWidth, rectWidth / cssZoom)
  const layoutHeight = isDocumentRoot
    ? firstPositive(borderLayoutHeight, innerLayoutHeight, rectHeight / cssZoom)
    : firstPositive(innerLayoutHeight, borderLayoutHeight, rectHeight / cssZoom)

  // currentCSSZoom reports effective author zoom without mistaking transforms
  // for zoom. The ancestor walk, then measured ratio, keep this
  // bridge working in engines that predate that API.
  const scaleX = effectiveZoom || positiveNumber(
    rectWidth / (borderLayoutWidth || layoutWidth),
    1,
  )
  const scaleY = effectiveZoom || positiveNumber(
    rectHeight / (borderLayoutHeight || layoutHeight),
    1,
  )
  const clientLeft = finiteNumber(rect?.left)
    + finiteNumber(element?.clientLeft) * scaleX
  const clientTop = finiteNumber(rect?.top)
    + finiteNumber(element?.clientTop) * scaleY

  return {
    clientLeft,
    clientTop,
    clientWidth: layoutWidth * scaleX,
    clientHeight: layoutHeight * scaleY,
    width: layoutWidth,
    height: layoutHeight,
    scaleX,
    scaleY,
  }
}

/** Convert a viewport/client point into an element-local CSS-layout point. */
export function clientPointToLayout(point, space) {
  return {
    x: (finiteNumber(point?.x) - finiteNumber(space?.clientLeft))
      / positiveNumber(space?.scaleX, 1),
    y: (finiteNumber(point?.y) - finiteNumber(space?.clientTop))
      / positiveNumber(space?.scaleY, 1),
  }
}

/** Convert a viewport/client displacement into a CSS-layout displacement. */
export function clientDeltaToLayout(delta, space) {
  return {
    x: finiteNumber(delta?.x) / positiveNumber(space?.scaleX, 1),
    y: finiteNumber(delta?.y) / positiveNumber(space?.scaleY, 1),
  }
}
