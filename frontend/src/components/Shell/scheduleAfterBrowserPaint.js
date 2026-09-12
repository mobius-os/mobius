// Nested animation frames leave one browser paint opportunity between layout
// readiness and promotion without guessing at a device-specific timeout.
//
// Browsers pause animation frames while a tab is hidden. Some mobile/WebKit
// lifecycle paths can retire a queued frame while the document is suspended,
// so a one-shot pair must not be the sole owner of a launch-cover handoff.
// Re-arm the complete two-frame proof on every real foreground boundary: the
// destination still paints once beneath its cover, and a discarded callback
// can never strand that cover over an otherwise healthy shell.
export function scheduleAfterBrowserPaint(
  callback,
  requestFrame = requestAnimationFrame,
  cancelFrame = cancelAnimationFrame,
  {
    documentTarget = typeof document === 'undefined' ? null : document,
    windowTarget = typeof window === 'undefined' ? null : window,
  } = {},
) {
  let frame = null
  let generation = 0
  let settled = false

  const visible = () => documentTarget?.visibilityState !== 'hidden'

  function cancelPendingFrame() {
    generation += 1
    if (frame !== null) cancelFrame(frame)
    frame = null
  }

  function removeLifecycleListeners() {
    documentTarget?.removeEventListener?.('visibilitychange', onVisibilityChange)
    windowTarget?.removeEventListener?.('pageshow', onPageShow)
  }

  function finish() {
    if (settled) return
    settled = true
    frame = null
    removeLifecycleListeners()
    callback()
  }

  function schedule() {
    if (settled || !visible()) return
    cancelPendingFrame()
    const owner = generation
    frame = requestFrame(() => {
      if (settled || owner !== generation || !visible()) return
      frame = requestFrame(() => {
        if (settled || owner !== generation || !visible()) return
        finish()
      })
    })
  }

  function onVisibilityChange() {
    if (!visible()) {
      cancelPendingFrame()
      return
    }
    schedule()
  }

  function onPageShow() {
    if (visible()) schedule()
  }

  documentTarget?.addEventListener?.('visibilitychange', onVisibilityChange)
  windowTarget?.addEventListener?.('pageshow', onPageShow)
  schedule()

  return () => {
    if (settled) return
    settled = true
    cancelPendingFrame()
    removeLifecycleListeners()
  }
}
