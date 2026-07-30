const STANDALONE_BOOT_SLOT = '__mobius-standalone-app__'

function validBootRecord(value) {
  return value
    && Number.isInteger(value.id)
    && value.id > 0
    && typeof value.slug === 'string'
    && value.slug.length > 0
    && typeof value.name === 'string'
}

/**
 * Read the server-authored app identity that selects the minimal standalone
 * host. The URL alone is not authority: only the dedicated backend route emits
 * this slot, so an arbitrary SPA fallback cannot trick the shell bundle into
 * becoming an app host for guessed metadata.
 */
export function readStandaloneBoot(doc = globalThis.document) {
  try {
    const raw = doc?.getElementById?.(STANDALONE_BOOT_SLOT)?.textContent?.trim()
    if (!raw) return null
    const value = JSON.parse(raw)
    return validBootRecord(value) ? value : null
  } catch {
    return null
  }
}

export function standaloneAppVersion(app) {
  return typeof app?.updated_at === 'string' && app.updated_at
    ? app.updated_at
    : '0'
}

export function initiallyOpenStandaloneInstallCard({
  installState, forceOpen = false, dismissed = false,
}) {
  return forceOpen || (installState !== 'installed' && !dismissed)
}

export function standaloneInstallCompleted(previousState, currentState) {
  return previousState !== 'installed' && currentState === 'installed'
}

export function isVisualContentOnly(storage = globalThis.sessionStorage) {
  try {
    return storage?.getItem?.('mobius:visual-content-only') === '1'
  } catch {
    return false
  }
}
