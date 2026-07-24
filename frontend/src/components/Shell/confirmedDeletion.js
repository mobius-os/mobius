/* Confirmed-deletion reconciliation for offline-capable drawer lists.
 *
 * A list read can be a stale service-worker fallback, so absence is never
 * deletion evidence. The inverse is equally important: once a DELETE (or a
 * system event from another tab) confirms a row is gone, a later stale list
 * must not resurrect it. Shell owns these session-scoped tombstones and clears
 * one only after recovery succeeds.
 */

function normalizedId(value) {
  return value == null ? null : String(value)
}

export function rememberConfirmedDeletion(deletedIds, id) {
  const key = normalizedId(id)
  if (key == null || !deletedIds) return
  deletedIds.add(key)
}

export function forgetConfirmedDeletion(deletedIds, id) {
  const key = normalizedId(id)
  if (key == null || !deletedIds) return
  deletedIds.delete(key)
}

export function withoutConfirmedDeletions(rows, deletedIds) {
  const list = Array.isArray(rows) ? rows : []
  if (!deletedIds?.size) return list
  return list.filter(row => !deletedIds.has(normalizedId(row?.id)))
}

/**
 * Clear a prior deletion only when a direct resource probe proves that the id
 * is live again. Apps use reusable integer ids, and reinstall revives a
 * tombstoned row through the generic app_updated event. A list response cannot
 * establish either case because its offline fallback may still contain the
 * deleted predecessor.
 */
export async function forgetConfirmedDeletionIfExists(
  deletedIds,
  id,
  probe,
) {
  const key = normalizedId(id)
  if (key == null || !deletedIds?.has(key) || typeof probe !== 'function') {
    return false
  }
  let verdict
  try {
    verdict = await probe(key)
  } catch {
    return false
  }
  if (verdict !== 'exists') return false
  deletedIds.delete(key)
  return true
}
