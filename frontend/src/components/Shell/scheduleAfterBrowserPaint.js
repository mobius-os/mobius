// requestAnimationFrame callbacks run before their frame is painted. The
// second callback therefore follows one complete browser paint opportunity.
// This is the smallest deterministic boundary for preparing a hidden visual
// layer without guessing at a device-specific timeout.
export function scheduleAfterBrowserPaint(
  callback,
  requestFrame = requestAnimationFrame,
  cancelFrame = cancelAnimationFrame,
) {
  let active = true
  let firstFrame = 0
  let secondFrame = 0

  firstFrame = requestFrame(() => {
    if (!active) return
    firstFrame = 0
    secondFrame = requestFrame(() => {
      if (!active) return
      secondFrame = 0
      active = false
      callback()
    })
  })

  return () => {
    if (!active) return
    active = false
    if (firstFrame) cancelFrame(firstFrame)
    if (secondFrame) cancelFrame(secondFrame)
  }
}
