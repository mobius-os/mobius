export const COMPOSER_TEXTAREA_MAX_HEIGHT = 280
export const COMPOSER_TEXTAREA_TALL_THRESHOLD = 45

function composerPill(textarea) {
  return textarea?.closest?.('.chat__pill') || null
}

/**
 * Reconcile the textarea's inline height with its current DOM value.
 *
 * Composer text changes through more than the input event: send cleanup,
 * failed-send reconciliation, voice input, restored drafts, and browser
 * foregrounding can all update or restore it. Keeping this operation shared
 * prevents an empty textarea from retaining a previous multi-line height.
 */
export function resizeComposerTextarea(textarea) {
  if (!textarea?.style) return 0

  textarea.style.height = 'auto'
  const measured = Number(textarea.scrollHeight) || 0

  // Retained workspace panes can be display:none while React commits a state
  // update. Their scrollHeight is 0, which is not useful geometry; leave the
  // intrinsic one-row height in place and reconcile when the pane is visible.
  if (measured <= 0) {
    if (!textarea.value) composerPill(textarea)?.classList?.remove('chat__pill--tall')
    return 0
  }

  const height = Math.min(measured, COMPOSER_TEXTAREA_MAX_HEIGHT)
  textarea.style.height = `${height}px`
  composerPill(textarea)?.classList?.toggle(
    'chat__pill--tall',
    height > COMPOSER_TEXTAREA_TALL_THRESHOLD,
  )
  return height
}

/** Collapse immediately while React is still committing an empty value. */
export function resetComposerTextarea(textarea) {
  if (!textarea?.style) return
  textarea.style.height = 'auto'
  composerPill(textarea)?.classList?.remove('chat__pill--tall')
}
