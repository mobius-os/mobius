/**
 * Progressive access to the imperative browser-owned PWA install surface.
 *
 * This module deliberately owns no UI and no fallback navigation. Callers can
 * try navigator.install(), then keep their established app-specific flow when
 * it is absent or fails.
 */

export function supportsWebInstall(
  navigatorObject = typeof navigator !== 'undefined' ? navigator : null,
) {
  return typeof navigatorObject?.install === 'function'
}

export async function webInstallPermissionState(
  navigatorObject = typeof navigator !== 'undefined' ? navigator : null,
) {
  if (typeof navigatorObject?.permissions?.query !== 'function') return 'unknown'
  try {
    const status = await navigatorObject.permissions.query({
      name: 'web-app-installation',
    })
    return ['granted', 'prompt', 'denied'].includes(status?.state)
      ? status.state
      : 'unknown'
  } catch {
    // The API and its permission descriptor are both experimental and may
    // ship independently. An unrecognised descriptor must not block install.
    return 'unknown'
  }
}

export function resolveInstallManifestUrl(manifestUrl, baseUrl) {
  const base = baseUrl ||
    (typeof document !== 'undefined' ? document.baseURI : undefined)
  return new URL(manifestUrl, base).href
}

export async function requestManifestWebInstall({
  manifestUrl,
  navigatorObject = typeof navigator !== 'undefined' ? navigator : null,
  baseUrl,
  permissionState = 'unknown',
}) {
  if (!supportsWebInstall(navigatorObject)) {
    return { outcome: 'unsupported' }
  }

  // Accept a state read before the click, rather than querying here: adding an
  // awaited permission read between the click and install() could itself use
  // up the short transient-activation window this API requires.
  if (permissionState === 'denied') {
    return { outcome: 'blocked' }
  }

  try {
    // Möbius manifests always declare a stable `id`, so the still-evolving
    // optional manifestId parameter is intentionally omitted. The browser
    // fetches and validates the declared identity itself.
    await navigatorObject.install({
      manifest: resolveInstallManifestUrl(manifestUrl, baseUrl),
    })
    return { outcome: 'accepted' }
  } catch (error) {
    const errorName = typeof error?.name === 'string' ? error.name : 'Error'
    if (errorName === 'AbortError') {
      // Chromium currently uses AbortError for both a one-off cancellation
      // and a persisted host-permission denial. Re-read the owning permission
      // so callers can redirect a blocked host instead of offering a dead retry.
      const permission = await webInstallPermissionState(navigatorObject)
      return { outcome: permission === 'denied' ? 'blocked' : 'dismissed' }
    }
    return { outcome: 'failed', errorName }
  }
}
