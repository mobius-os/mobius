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

// One reader for the whole page load. App and useNavigation share this parsed
// value; a second storage read would see the already-removed key.
export const shellReload = consumeShellReload()
