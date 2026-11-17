import { markExplicitSignOut } from './mobiusSignIn.js'

/**
 * Revoke installation handoffs, stop this phone's native push, and stop its
 * location tracking while the owner credential still exists, then remove every
 * local owner-scoped trace. Each server call is best effort so a broken or
 * offline transport can delay logout only until its request deadline; they can
 * never strand the ordinary local sign-out path.
 */
export async function clearExplicitOwnerSession({
  stopInstallHandoffPreparation,
  revokeInstallHandoffs,
  forgetPushDevice,
  stopLocationTracking,
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
  try {
    await forgetPushDevice()
  } catch {
    // A phone left registered is retired by the relay's invalid-token reply
    // or by the next owner's registration of the same token.
  }
  try {
    await stopLocationTracking()
  } catch {
    // The phone corrects its own tracking report the next time it opens.
  }
  dropCredential()
  // Tell the sign-in screen this was deliberate, so a mobius.you instance
  // does not redirect straight back into a fresh session.
  markExplicitSignOut()
  await clearOwnerCache()
}
