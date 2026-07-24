export const COMPOSER_TEXTAREA_MAX_HEIGHT = 280
export const COMPOSER_TEXTAREA_TALL_THRESHOLD = 45
let nativeSizingSupport

function composerPill(textarea) {
  return textarea?.closest?.('.chat__pill') || null
}

export function composerUsesNativeSizing(css = globalThis.CSS) {
  // An injected CSS object keeps the capability boundary directly testable.
  // Cache only the real browser verdict; tests and non-browser runtimes should
  // not freeze a synthetic result for later calls.
  if (css !== globalThis.CSS) {
    return !!css?.supports?.('field-sizing', 'content')
  }
  if (nativeSizingSupport === undefined) {
    nativeSizingSupport = !!css?.supports?.('field-sizing', 'content')
  }
  return nativeSizingSupport
}

export function syncComposerTallClass(
  textarea,
  height = textarea?.offsetHeight,
) {
  const measured = Number(height) || 0
  composerPill(textarea)?.classList?.toggle(
    'chat__pill--tall',
    measured > COMPOSER_TEXTAREA_TALL_THRESHOLD,
  )
  return measured
}

/**
 * Reconcile the textarea's inline height with its current DOM value.
 *
 * Composer text changes through more than the input event: send cleanup,
 * failed-send reconciliation, voice input, restored drafts, and browser
 * foregrounding can all update or restore it. Keeping this operation shared
 * prevents an empty textarea from retaining a previous multi-line height.
 */
export function resizeComposerTextarea(textarea, value = textarea?.value) {
  if (!textarea?.style) return 0
  // Current browsers can own content sizing entirely in CSS. Avoid touching
  // height or reading scrollHeight on the keystroke path: that read forces a
  // document layout, whose cost grows with every mounted drawer/transcript
  // row. ResizeObserver in ChatInputBar updates the tall alignment only when
  // the browser reports an actual size change.
  if (composerUsesNativeSizing()) return 0

  // Empty is a semantic one-line state, not a geometry question. During a
  // multi-pane mount / foreground transition Chromium can briefly report an
  // empty textarea's scrollHeight as its old or available flex height (often
  // the 280px cap). Measuring that transient value makes the blank composer
  // fill the pane until the next keystroke. Reset deterministically instead.
  if (value === '') {
    resetComposerTextarea(textarea)
    return 0
  }

  textarea.style.height = 'auto'
  const measured = Number(textarea.scrollHeight) || 0

  // Retained workspace panes can be display:none while React commits a state
  // update. Their scrollHeight is 0, which is not useful geometry; leave the
  // intrinsic one-row height in place and reconcile when the pane is visible.
  if (measured <= 0) return 0

  const height = Math.min(measured, COMPOSER_TEXTAREA_MAX_HEIGHT)
  textarea.style.height = `${height}px`
  syncComposerTallClass(textarea, height)
  return height
}

/** Collapse immediately while React is still committing an empty value. */
export function resetComposerTextarea(textarea) {
  if (!textarea?.style) return
  textarea.style.height = composerUsesNativeSizing() ? '' : 'auto'
  composerPill(textarea)?.classList?.remove?.('chat__pill--tall')
}

/**
 * Reconcile authoritative composer state with browser-owned geometry.
 *
 * Native `field-sizing` removes the per-keystroke measurement path, but it
 * cannot clear stale inline form geometry restored by Chromium. Empty remains
 * a semantic one-line state, so clear it explicitly; non-empty content can use
 * the native or measured sizing path above.
 */
export function reconcileComposerTextarea(
  textarea,
  value = textarea?.value,
) {
  if (!value) {
    resetComposerTextarea(textarea)
    return 0
  }
  return resizeComposerTextarea(textarea, value)
}
