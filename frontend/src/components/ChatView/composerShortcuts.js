export function resolveComposerEnterAction(event, {
  hasInput = false,
  canSteer = false,
  canRequestSteer = canSteer,
  canSubmitSteer = canRequestSteer,
  isTouchPrimary = false,
} = {}) {
  if (!event || event.key !== 'Enter' || event.shiftKey) return null

  const modifiedEnter = !!(event.metaKey || event.ctrlKey)
  if (!modifiedEnter && isTouchPrimary) return null

  if (hasInput) {
    if (modifiedEnter && canSubmitSteer) return 'submit-steer'
    return 'submit'
  }
  if (canRequestSteer) return 'steer'
  return 'noop'
}

/** Whether an inline chat editor (the QA card's custom answer, the queued-
 * message editor) should treat this Enter as "send". Reuses the composer's own
 * decision so every inline editor sends on the same chord: plain Enter on
 * desktop, Shift+Enter always a newline, Enter a newline on touch, and
 * Cmd/Ctrl+Enter always sends. These editors have no queued-steer affordance,
 * so only a real submit counts. */
export function isInlineEditorSubmit(event, { isTouchPrimary = false } = {}) {
  const action = resolveComposerEnterAction(event, { hasInput: true, isTouchPrimary })
  return action === 'submit' || action === 'submit-steer'
}

/** Paste-without-formatting chord. ClipboardEvent does not reliably retain
 * keyboard modifiers, so the composer snapshots this during keydown and lets
 * the subsequent paste event consume it. */
export function isPlainTextPasteShortcut(event) {
  return !!(
    String(event?.key || '').toLowerCase() === 'v'
    && event?.shiftKey
    && (event?.metaKey || event?.ctrlKey)
  )
}
