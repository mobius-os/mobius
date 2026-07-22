// Interpret the settings response before consulting request freshness. A save
// can become stale while it waits behind a later edit, but an HTTP failure is
// still a real failure -- especially when the save also carries the provider
// selected by a just-completed authentication flow.
export async function settleBackgroundAgentSave(response, isStale) {
  if (!response?.ok) {
    let detail = ''
    try { detail = (await response?.json?.())?.detail || '' } catch {}
    throw new Error(detail || 'Could not save background agents.')
  }
  return { stale: Boolean(isStale?.()) }
}
