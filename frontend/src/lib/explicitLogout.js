/**
 * Revoke installation handoffs while the owner credential still exists, then
 * remove every local owner-scoped trace. Revocation is best effort so a broken
 * or offline transport can delay logout only until its request deadline; it
 * can never strand the ordinary local sign-out path.
 */
export async function clearExplicitOwnerSession({
  stopInstallHandoffPreparation,
  revokeInstallHandoffs,
  dropCredential,
  clearOwnerCache,
}) {
  try {
    await stopInstallHandoffPreparation()
  } catch {
    // Continue to the server-owned invalidation boundary.
  }
  try {
    await revokeInstallHandoffs()
  } catch {
    // Local privacy cleanup and sign-out remain authoritative when offline.
  }
  dropCredential()
  await clearOwnerCache()
}
