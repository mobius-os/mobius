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

/**
 * Translate one browser traversal into the ordered app-runtime commands that
 * make its logical stack match. Browser UI can jump across several entries at
 * once, so this deliberately returns every required command rather than
 * assuming popstate always moves by one.
 */
export function reconcileStandaloneHistory(
  currentEntries,
  destinationState,
  { localPopPending = false } = {},
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
      commands.push({ direction: 'back', requestId: entry?.requestId ?? null })
    }
  } else if (entries.length > current.length) {
    for (const entry of entries.slice(current.length)) {
      commands.push({ direction: 'forward', requestId: entry?.requestId ?? null })
    }
  }

  return { entries, commands, consumedLocalPop }
}

export { MAX_STANDALONE_HISTORY_ENTRIES }
