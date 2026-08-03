// Nested animation frames leave one browser paint opportunity between layout
// readiness and promotion without guessing at a device-specific timeout.
export function scheduleAfterBrowserPaint(
  callback,
  requestFrame = requestAnimationFrame,
  cancelFrame = cancelAnimationFrame,
) {
  let frame = requestFrame(() => {
    frame = requestFrame(callback)
  })

  return () => cancelFrame(frame)
}
