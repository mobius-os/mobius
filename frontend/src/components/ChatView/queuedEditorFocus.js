export function restoreQueuedEditorAfterSave(outcome, editor) {
  if (outcome === 'saved' || !editor) return false
  try { editor.focus({ preventScroll: true }) } catch { editor.focus() }
  return true
}
