/* Bridge viewport/client geometry into the CSS layout space that inline styles consume. */

function finiteNumber(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) ? number : fallback
}

function positiveNumber(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

function layoutZoom(element) {
  const current = positiveNumber(element.currentCSSZoom)
  if (current) return current
  if (typeof getComputedStyle !== 'function') return 1
  return positiveNumber(
    getComputedStyle(element.ownerDocument.documentElement).zoom,
    1,
  )
}

/**
 * Capture an element's padding box (the document box for <html>) in CSS-layout
 * coordinates. Möbius has one uniform zoom policy on the document root; this
 * is the single boundary where client input crosses into that layout space.
 */
export function captureLayoutSpace(element) {
  const rect = element.getBoundingClientRect()
  const zoom = layoutZoom(element)
  const isDocumentRoot = element.ownerDocument?.documentElement === element
  const width = positiveNumber(
    isDocumentRoot ? element.offsetWidth : element.clientWidth,
    positiveNumber(
      isDocumentRoot ? element.clientWidth : element.offsetWidth,
      finiteNumber(rect.width) / zoom,
    ),
  )
  const height = positiveNumber(
    isDocumentRoot ? element.offsetHeight : element.clientHeight,
    positiveNumber(
      isDocumentRoot ? element.clientHeight : element.offsetHeight,
      finiteNumber(rect.height) / zoom,
    ),
  )

  return {
    clientLeft: finiteNumber(rect.left) + finiteNumber(element.clientLeft) * zoom,
    clientTop: finiteNumber(rect.top) + finiteNumber(element.clientTop) * zoom,
    width,
    height,
    zoom,
  }
}

/** Convert a viewport/client point into an element-local CSS-layout point. */
export function clientPointToLayout(point, space) {
  const zoom = positiveNumber(space?.zoom, 1)
  return {
    x: (finiteNumber(point?.x) - finiteNumber(space?.clientLeft)) / zoom,
    y: (finiteNumber(point?.y) - finiteNumber(space?.clientTop)) / zoom,
  }
}

/** Convert a viewport/client length into a CSS-layout length. */
export function clientLengthToLayout(value, space) {
  return finiteNumber(value) / positiveNumber(space?.zoom, 1)
}
