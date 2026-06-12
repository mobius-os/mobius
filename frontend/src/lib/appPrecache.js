/* Pure helpers for warming the service worker's app-code cache (the
 * per-app frame + module) BEFORE the user's first open, so opening an
 * app is a cache read instead of a network round trip.
 *
 * Two signals feed the warm set:
 *   - the drawer pin (`app.pinned_at`) — deliberate, long-term intent;
 *   - the iframe LRU in Shell.jsx — actual recent use. The in-memory
 *     LRU starts empty on every load, so its rotations are merged into
 *     localStorage (mergeAppLru) and read back on the next shell load
 *     (parseStoredAppLru). That persisted list is the cross-session
 *     "most-recent apps" signal.
 *
 * Kept free of imports so the selection rules unit-test under plain
 * `node --test` (the swCachePolicy.test.js pattern); the impure parts
 * (localStorage, requestIdleCallback, postMessage to the SW) live in
 * Shell.jsx.
 */

export const APP_LRU_STORAGE_KEY = 'mobius-app-lru'

// Upper bound on apps warmed per shell load AND on the persisted LRU
// depth. Each warmed app costs one token fetch + two bounded SW fetches
// (frame + module), all idle-scheduled — six keeps the whole pass cheap
// on a phone while covering pins + everything the user touched lately.
export const WARM_APP_LIMIT = 6

// Tolerant read of the persisted LRU. localStorage survives across
// releases, so the raw value can be anything an older build (or junk)
// left behind — never throw, just degrade to "no recency signal".
export function parseStoredAppLru(raw) {
  if (!raw) return []
  try {
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      id => typeof id === 'number' || typeof id === 'string',
    )
  } catch {
    return []
  }
}

// Folds the live in-memory LRU into the previously stored one: current
// entries first (they are the freshest recency), then stored entries
// that haven't reappeared, deduped, capped. The merge — rather than a
// plain overwrite — is what gives the list cross-session depth: the
// in-memory LRU holds only 4 ids, so overwriting would forget last
// session's apps the moment one app is opened today.
export function mergeAppLru(current, stored, cap = WARM_APP_LIMIT) {
  const merged = []
  const seen = new Set()
  for (const id of [...(current || []), ...(stored || [])]) {
    const key = String(id)
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(id)
    if (merged.length >= cap) break
  }
  return merged
}

// Picks which installed apps to warm: recent first (in LRU order — the
// stronger predictor of the next open), then pinned apps by newest pin,
// deduped, capped at `limit`. Ids in `recentIds` that aren't in the
// live `apps` list (uninstalled since last session) are skipped, so a
// stale persisted LRU can never warm a dead route.
export function selectAppsToWarm(apps, recentIds, limit = WARM_APP_LIMIT) {
  const byId = new Map((apps || []).map(a => [String(a.id), a]))
  const picked = []
  const seen = new Set()
  const take = (app) => {
    const key = String(app.id)
    if (seen.has(key) || picked.length >= limit) return
    seen.add(key)
    picked.push(app)
  }
  for (const id of recentIds || []) {
    const app = byId.get(String(id))
    if (app) take(app)
  }
  const pinned = (apps || [])
    .filter(a => a.pinned_at)
    .sort((a, b) => String(b.pinned_at).localeCompare(String(a.pinned_at)))
  for (const app of pinned) take(app)
  return picked
}
