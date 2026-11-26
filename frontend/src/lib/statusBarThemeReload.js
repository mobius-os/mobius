/** Saving a theme, then refreshing an installed iPhone app's status bar. */

import { isInstalledIosApp } from '../utils/installPlatform.js'

/**
 * Installed iOS re-reads its opaque status-bar colour only when the document
 * loads or returns to the foreground (Android installs recolour live).
 */
export function needsStatusBarReload(win = globalThis.window) {
  return isInstalledIosApp(win)
}

/**
 * Save the theme, then reload only where the status bar needs it. A failed
 * save rejects before any reload; a failed reload never rejects, so it can
 * never roll back a theme the server already holds.
 */
export async function saveThemeThenRefreshStatusBar({ save, reload, win = globalThis.window }) {
  await save()
  if (typeof reload !== 'function' || !needsStatusBarReload(win)) return false
  try {
    await reload()
  } catch {
    // The theme is saved and painted; the bar catches up at the next launch.
  }
  return true
}
