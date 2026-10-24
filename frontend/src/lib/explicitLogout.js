import { markExplicitSignOut } from './mobiusSignIn.js'

/**
 * Revoke installation handoffs and stop this phone's native push while the
 * owner credential still exists, then remove every local owner-scoped trace.
 * Both server calls are best effort so a broken or offline transport can delay
 * logout only until its request deadline; they can never strand the ordinary
 * local sign-out path.
 */
export async function clearExplicitOwnerSession({
  stopInstallHandoffPreparation,
  revokeInstallHandoffs,
  forgetPushDevice,
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
  dropCredential()
  // Tell the sign-in screen this was deliberate, so a mobius.you instance
  // does not redirect straight back into a fresh session.
  markExplicitSignOut()
  await clearOwnerCache()
}
