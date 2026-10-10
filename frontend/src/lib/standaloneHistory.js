import { isCurrentRetiredAppEntry, isRetiredAppEntry } from './navHistory.js'

const MAX_STANDALONE_HISTORY_ENTRIES = 40

function normalizeEntry(value) {
  if (!value || typeof value !== 'object') return null
  return {
    requestId: typeof value.requestId === 'string' ? value.requestId : null,
    reversible: value.reversible === true,
  }
}

function normalizeDepth(value) {
  const depth = Number(value)
  if (!Number.isInteger(depth) || depth < 0) return 0
  return Math.min(depth, MAX_STANDALONE_HISTORY_ENTRIES)
}

function normalizedEntries(values) {
  if (!Array.isArray(values)) return null
  return values
    .slice(0, MAX_STANDALONE_HISTORY_ENTRIES)
    .map(normalizeEntry)
}

/**
 * Read the standalone app's logical stack from a browser-history entry.
 *
 * Older installs stored only a depth and the newest entry. `currentEntries`
 * lets a traversal through those entries preserve the portion of the stack we
 * already know, while new writes carry the complete stack.
 */
export function readStandaloneHistoryEntries(state, currentEntries = []) {
  const complete = normalizedEntries(state?.mobiusStandaloneEntries)
  if (complete) return complete

  const depth = normalizeDepth(state?.mobiusStandaloneDepth)
  const current = normalizedEntries(currentEntries) || []
  const entries = current.slice(0, depth)
  while (entries.length < depth) entries.push(null)

  const newest = normalizeEntry(state?.mobiusStandaloneEntry)
  if (newest && entries.length) entries[entries.length - 1] = newest
  return entries
}

/** Preserve unrelated history state while writing both the complete stack and
 * the two legacy fields needed by an older bundle after a rollback. */
export function standaloneHistoryState(state, entries) {
  const complete = normalizedEntries(entries) || []
  return {
    ...(state && typeof state === 'object' ? state : {}),
    mobiusStandaloneEntries: complete,
    mobiusStandaloneDepth: complete.length,
    mobiusStandaloneEntry: complete.at(-1) || null,
  }
}

/** Push an app level, or replace a retired level proven to be physically current.
 * Return the new logical stack only after the browser accepted the write. */
export function pushStandaloneHistoryEntry(history, entries, registry, appId, meta = {}, url = '') {
  const current = entries.at(-1)
  const physical = readStandaloneHistoryEntries(history.state)
  const reuse = physical.length === entries.length && isCurrentRetiredAppEntry(
    registry.get(current?.requestId), current?.requestId, physical.at(-1)?.requestId,
  )
  if (!reuse && entries.length >= MAX_STANDALONE_HISTORY_ENTRIES) return null
  const entry = {
    requestId: typeof meta.requestId === 'string' ? meta.requestId : null,
    reversible: meta.reversible === true,
  }
  const next = reuse ? [...entries.slice(0, -1), entry] : [...entries, entry]
  try {
    history[reuse ? 'replaceState' : 'pushState'](standaloneHistoryState(history.state, next), '', url)
  } catch {
    return null
  }
  registry.set(entry.requestId, { appId: String(appId), status: 'live' })
  return next
}

/**
 * Translate one browser traversal into the ordered app-runtime commands that
 * make its logical stack match. Browser UI can jump across several entries at
 * once, so this deliberately returns every required command rather than
 * assuming popstate always moves by one.
 */
export function reconcileStandaloneHistory(
  currentEntries,
  destinationState,
  { localPopPending = false, registry = null } = {},
) {
  const current = normalizedEntries(currentEntries) || []
  const entries = readStandaloneHistoryEntries(destinationState, current)
  const commands = []
  let consumedLocalPop = false

  if (entries.length < current.length) {
    const removed = current.slice(entries.length).reverse()
    for (const entry of removed) {
      if (localPopPending && !consumedLocalPop) {
        consumedLocalPop = true
        continue
      }
      if (!registry || !isRetiredAppEntry(registry.get(entry?.requestId))) {
        commands.push({ direction: 'back', requestId: entry?.requestId ?? null })
      }
    }
  } else if (entries.length > current.length) {
    for (const entry of entries.slice(current.length)) {
      if (!registry || !isRetiredAppEntry(registry.get(entry?.requestId))) {
        commands.push({ direction: 'forward', requestId: entry?.requestId ?? null })
      }
    }
  }

  const direction = entries.length > current.length ? 'forward' : 'back'
  const skipRetired = registry !== null && entries.length > 0
    && isRetiredAppEntry(registry.get(entries.at(-1)?.requestId))
  return { entries, commands, consumedLocalPop, direction, skipRetired }
}

export { MAX_STANDALONE_HISTORY_ENTRIES }
