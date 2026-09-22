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

export function resolveInstallManifestUrl(manifestUrl, baseUrl) {
  const base = baseUrl ||
    (typeof document !== 'undefined' ? document.baseURI : undefined)
  return new URL(manifestUrl, base).href
}

export async function requestManifestWebInstall({
  manifestUrl,
  navigatorObject = typeof navigator !== 'undefined' ? navigator : null,
  baseUrl,
}) {
  if (!supportsWebInstall(navigatorObject)) {
    return { outcome: 'unsupported' }
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
    if (errorName === 'AbortError') return { outcome: 'dismissed' }
    return { outcome: 'failed', errorName }
  }
}
