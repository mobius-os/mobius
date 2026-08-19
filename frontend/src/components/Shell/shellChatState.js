/** Reconcile durable drawer state after browser suspension or process restart. */
export function watchChatStateOnResume({ doc, win, reconcile } = {}) {
  if (!doc || typeof reconcile !== 'function') return () => {}
  let disposed = false
  let inFlight = null
  const run = () => {
    if (disposed || doc.visibilityState !== 'visible') return inFlight
    if (inFlight) return inFlight
    inFlight = Promise.resolve().then(reconcile).catch(() => {}).finally(() => {
      inFlight = null
    })
    return inFlight
  }
  doc.addEventListener('visibilitychange', run)
  win?.addEventListener?.('focus', run)
  win?.addEventListener?.('pageshow', run)
  win?.addEventListener?.('online', run)
  return () => {
    disposed = true
    doc.removeEventListener('visibilitychange', run)
    win?.removeEventListener?.('focus', run)
    win?.removeEventListener?.('pageshow', run)
    win?.removeEventListener?.('online', run)
  }
}
