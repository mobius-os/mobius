/**
 * Progressive access to the incubating browser-owned PWA install surfaces.
 *
 * This module deliberately owns no UI and no fallback navigation. Callers can
 * try the strongest available browser primitive, then keep their existing
 * beforeinstallprompt/manual flow when it is absent or fails.
 */

export function supportsWebInstall(
  navigatorObject = typeof navigator !== 'undefined' ? navigator : null,
) {
  return typeof navigatorObject?.install === 'function'
}

export function supportsManifestInstallElement(
  windowObject = typeof window !== 'undefined' ? window : null,
) {
  const ElementClass = windowObject?.HTMLInstallElement
  // The first origin-trial design used `installurl`; the current design uses
  // a direct manifest URL. Only select the element when that exact contract
  // exists, otherwise the established fallback remains authoritative.
  return typeof ElementClass === 'function' &&
    ElementClass.prototype != null &&
    'manifest' in ElementClass.prototype
}

export function preferredDirectInstallMode({
  windowObject = typeof window !== 'undefined' ? window : null,
  navigatorObject = typeof navigator !== 'undefined' ? navigator : null,
} = {}) {
  if (supportsManifestInstallElement(windowObject)) return 'element'
  if (supportsWebInstall(navigatorObject)) return 'api'
  return null
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
