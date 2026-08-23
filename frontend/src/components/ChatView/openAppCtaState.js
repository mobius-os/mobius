/* Eligibility policy shared by Open-app rendering and its retirement clock. */

export function shouldShowOpenAppCta(builtApp, turnActive = false) {
  if (!builtApp?.id) return false
  const seenCurrentBuild = Boolean(
    builtApp.updated_at
    && builtApp.preview_seen_updated_at === builtApp.updated_at
  )
  if (!seenCurrentBuild) return true
  // Opening during the live turn acknowledges that preview only. The settled
  // result surfaces once more even if the last source write happened before
  // the turn ended; opening it then is the durable final acknowledgement.
  return !turnActive && !builtApp.preview_seen_final
}
