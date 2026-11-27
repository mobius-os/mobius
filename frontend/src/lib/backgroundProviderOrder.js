/* Background-provider priority: the owner's saved order versus what Settings shows.

   The saved list is the owner's order and is persisted as-is. Settings only
   *displays* connected providers first, so a provider that is temporarily
   signed out (an expired token, say) keeps its saved slot and gets it back on
   reconnect. The backend already skips unconnected providers when it picks a
   background provider, so the display grouping never needs to be saved. */

/** Display order: connected rows first, each group in saved order. Never persist this. */
export function connectedProvidersFirst(rows, configuredProviders) {
  const connected = []
  const disconnected = []
  for (const row of rows) {
    if (configuredProviders.has(row.provider)) connected.push(row)
    else disconnected.push(row)
  }
  return [...connected, ...disconnected]
}

export function hasActiveConnectedProvider(rows, configuredProviders) {
  return rows.some(row => configuredProviders.has(row.provider) && row.enabled !== false)
}

/**
 * True when an edit would switch off the last enabled connected provider.
 *
 * The backend refuses an all-disabled list but, when every *connected* row is
 * disabled, falls through to the first connected row regardless of `enabled`.
 * Settings is therefore the only guard for this case. An edit made while no
 * connected provider is active (the active one was signed out) is not refused,
 * so the owner can still reorder or re-enable.
 */
export function disablesLastConnectedProvider(current, next, configuredProviders) {
  return hasActiveConnectedProvider(current, configuredProviders)
    && !hasActiveConnectedProvider(next, configuredProviders)
}

/**
 * Move a connected row between two *display* positions and return the new
 * saved order, or null when either position is outside the connected group.
 * Connected rows trade places only among the slots they already hold in the
 * saved order; disconnected rows keep their saved positions untouched.
 */
export function moveConnectedProvider(rows, fromIndex, toIndex, configuredProviders) {
  const slots = []
  rows.forEach((row, index) => {
    if (configuredProviders.has(row.provider)) slots.push(index)
  })
  if (
    fromIndex < 0 || fromIndex >= slots.length
    || toIndex < 0 || toIndex >= slots.length
    || fromIndex === toIndex
  ) return null
  const connected = slots.map(index => rows[index])
  const [moved] = connected.splice(fromIndex, 1)
  connected.splice(toIndex, 0, moved)
  const next = [...rows]
  slots.forEach((slot, position) => { next[slot] = connected[position] })
  return next
}

// Gemini bills each token to the owner's own Google account or Cloud project,
// so connecting it does not hand it background work: its row starts as None
// until the owner chooses a model. A first provider still joins, because
// background work needs one.
const STARTS_OUT_OF_BACKGROUND = new Set(['gemini'])

/** The saved rows after `connected` (its row with model and effort) is newly connected. */
export function rowsAfterConnect(current, connected, { first }) {
  const row = { ...connected, enabled: first || !STARTS_OUT_OF_BACKGROUND.has(connected.provider) }
  if (first) return [row, ...current.filter(other => other.provider !== row.provider).map(other => ({ ...other, enabled: false }))]
  const known = current.some(other => other.provider === row.provider)
  return known ? current.map(other => other.provider === row.provider ? row : other) : [...current, row]
}
