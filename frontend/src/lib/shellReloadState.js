/**
 * Consumes the one-shot shell snapshot written immediately before a rebuild.
 *
 * This lives outside useNavigation so startup can inspect the snapshot without
 * importing the full shell/navigation graph into the initial bundle.
 */
export function consumeShellReload(storage) {
  try {
    const source = storage ?? sessionStorage
    const raw = source.getItem('shell-reload')
    if (!raw) return null
    source.removeItem('shell-reload')
    try { return JSON.parse(raw) } catch { return null }
  } catch {
    return null
  }
}

/**
 * Best-effort one-shot route handoff before a shell-generation reload.
 *
 * Session storage can be unavailable or full. Losing this convenience
 * snapshot may fall back to the ordinary persisted workspace, but it must
 * never cancel the reload itself and strand an installed app on an obsolete
 * or broken service-worker generation.
 */
export function writeShellReload(storage, value) {
  try {
    const target = storage ?? sessionStorage
    target.setItem('shell-reload', JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

// One reader for the whole page load. App and useNavigation share this parsed
// value; a second storage read would see the already-removed key.
export const shellReload = consumeShellReload()
